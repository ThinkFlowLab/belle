"""The shared Second Brain backend uses no harness library."""

import asyncio
import base64
import json
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest
from docx import Document
from openpyxl import Workbook

from belle_agent.second_brain.backend import UnreadableDocument
from belle_agent.second_brain.default_backend import SECOND_BRAIN_BACKEND
from belle_agent.second_brain.documents import parse_file
from belle_agent.second_brain.embedder import build_embedder
from belle_agent.second_brain.extract import extract_text
from belle_agent.second_brain.ingest import SecondBrainIngestService


async def test_document_readers_include_tables_and_sheet_names(tmp_path: Path) -> None:
    word = Document()
    word.add_heading("Harbour tides", 0)
    word.add_paragraph("Spring tides reach four metres.")
    table = word.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Port"
    table.cell(0, 1).text = "Height | m"
    table.cell(1, 0).text = "Gannet"
    table.cell(1, 1).text = "4"
    docx = tmp_path / "tides.docx"
    word.save(str(docx))
    text = await parse_file(docx)
    assert "# Harbour tides" in text and "| Port | Height \\| m |" in text
    assert "| Gannet | 4 |" in text

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Tides"
    sheet.append(["Port", None])
    sheet.append(["Gannet", 4])
    sheet.append([None, None])
    xlsx = tmp_path / "tides.xlsx"
    book.save(xlsx)
    text = await parse_file(xlsx)
    assert "## Tides" in text and "Column 2" in text and "| Gannet | 4 |" in text
    assert text.count("| Gannet | 4 |") == 1


async def test_spreadsheet_keeps_falsey_headers_and_uncached_formulas(tmp_path: Path) -> None:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append([0, False])
    sheet.append([1, True])
    sheet.append(["=SUM(1,2)", None])
    path = tmp_path / "values.xlsx"
    book.save(path)

    text = await parse_file(path)
    assert "| 0 | False |" in text
    assert "| 1 | True |" in text
    assert "| =SUM(1,2) |  |" in text


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("tides.csv", "Port,Height\nGannet,4\n", "| Gannet | 4 |"),
        ("tides.tsv", "Port\tHeight\nGannet\t4\n", "| Gannet | 4 |"),
        ("tides.json", '{"port":"Gannet","height":4}', '"port": "Gannet"'),
        (
            "tides.html",
            "<html><body><article><h1>Harbour tides</h1><p>"
            + "Spring tides reach four metres at Gannet harbour. " * 8
            + "</p></article></body></html>",
            "Spring tides reach four metres",
        ),
    ],
)
async def test_other_document_formats_keep_their_content(
    tmp_path: Path, name: str, content: str, expected: str
) -> None:
    path = tmp_path / name
    path.write_text(content)
    assert expected in await parse_file(path)


async def test_json_keeps_duplicate_keys_as_source_text(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    source = '{"id":1,"id":2}'
    path.write_text(source)
    assert await parse_file(path) == source


async def test_json_surrogate_escape_survives_ingestion(tmp_path: Path) -> None:
    source = r'{"a":"\ud800"}'
    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path / "corpus",
                                       backend=SECOND_BRAIN_BACKEND)
    result = await service.ingest_bytes(source.encode("utf-8"), "surrogate.json")
    assert result["status"] == "ingested"
    doc = service.corpus.get(result["doc_id"])
    assert doc is not None
    assert Path(doc.extract_path).read_text(encoding="utf-8") == source


async def test_delimited_reader_keeps_quoted_newlines(tmp_path: Path) -> None:
    path = tmp_path / "notes.csv"
    path.write_text('Name,Note\nGannet,"spring\ntide"\n')
    assert "| Gannet | spring tide |" in await parse_file(path)


@pytest.mark.parametrize(("suffix", "separator"), [(".csv", ","), (".tsv", "\t")])
async def test_delimited_reader_accepts_large_fields(tmp_path: Path, suffix: str, separator: str) -> None:
    path = tmp_path / f"large{suffix}"
    path.write_text(f"Key{separator}Value\nalpha{separator}{'x' * 131_073}\n")
    text = await parse_file(path)
    assert text.count("x") == 131_073


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("notes.md", b"# Caf\xe9"),
        ("notes.txt", "Hello".encode("utf-16-le")),
        ("table.csv", b"name\nCaf\xe9"),
        ("data.json", b'{"name":"Caf\xe9"}'),
        ("page.html", b"<p>Caf\xe9</p>"),
    ],
)
async def test_non_utf8_document_has_a_remembered_reason(tmp_path: Path, name: str, content: bytes) -> None:
    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path / "corpus",
                                       backend=SECOND_BRAIN_BACKEND)
    result = await service.ingest_bytes(content, name)
    assert result["status"] == "skipped_filtered"
    assert f"{name}: expected UTF-8 text" in result["reason"]
    again = await service.ingest_bytes(content, name)
    assert again["reason"] == result["reason"]


async def test_corrupt_word_document_has_a_file_specific_reason(tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a Word document")
    with pytest.raises(UnreadableDocument, match=r"broken.docx: cannot read it as .docx"):
        await parse_file(path)


def _pdf_with_text(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 20 50 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 100] "
         b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Root 1 0 R /Size {len(offsets)} >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(pdf)


async def test_pdf_text_layer_needs_no_parser_or_generated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "harbour.pdf"
    path.write_bytes(_pdf_with_text("Lamp rotation schedule"))
    monkeypatch.chdir(tmp_path)
    text, _blocks = await extract_text(path, parse_file=parse_file)
    assert "Lamp rotation schedule" in text
    assert {item.name for item in tmp_path.iterdir()} == {"harbour.pdf"}


async def test_unreadable_file_has_precise_reason_and_is_remembered(tmp_path: Path) -> None:
    path = tmp_path / "slides.pptx"
    path.write_bytes(b"not a presentation")
    with pytest.raises(UnreadableDocument, match=r"slides.pptx: Belle reads .* not .pptx"):
        await parse_file(path)
    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path / "corpus",
                                       backend=SECOND_BRAIN_BACKEND)
    result = await service.ingest_bytes(path.read_bytes(), path.name, "application/octet-stream")
    assert result["status"] == "skipped_filtered"
    assert "slides.pptx: Belle reads" in result["reason"]
    again = await service.ingest_bytes(path.read_bytes(), path.name, "application/octet-stream")
    assert again["status"] == "skipped_filtered" and again["reason"] == result["reason"]


def test_html_extraction_keeps_article_and_drops_navigation() -> None:
    page = "<html><head><title>Harbour</title></head><body><nav>Site menu</nav>"
    page += "<article><h1>Spring tides</h1><p>" + "Spring tides reach four metres at the harbour every year. " * 8
    page += "</p></article>"
    page += "</body></html>"
    title, text = SECOND_BRAIN_BACKEND.extract_html(page)
    assert title and "Spring tides reach four metres" in text and "Site menu" not in text


async def test_embedding_batches_and_decodes_both_forms() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.url.path == "/v1/embeddings" and "authorization" not in request.headers
        data = []
        for index, _ in enumerate(body["input"]):
            values = [float(index + 1), 2.0]
            embedding = (base64.b64encode(struct.pack("<2f", *values)).decode()
                         if len(seen) == 1 else values)
            data.append({"index": index, "embedding": embedding})
        return httpx.Response(200, json={"data": data})

    embedder = build_embedder({"api_base": "https://embed.test/v1/embeddings", "model": "embed"},
                              transport=httpx.MockTransport(handler))
    assert embedder is not None
    vectors = await embedder.embed_documents([str(i) for i in range(9)])
    assert len(vectors) == 9 and vectors[0] == [1.0, 2.0]
    assert [len(call["input"]) for call in seen] == [8, 1]
    assert await embedder.embed_query("test") == [1.0, 2.0]


async def test_embedding_query_does_not_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    embedder = build_embedder({"api_base": "https://embed.test/v1", "model": "embed"},
                              transport=httpx.MockTransport(handler))
    assert embedder is not None
    with pytest.raises(httpx.HTTPStatusError):
        await embedder.embed_query("test")
    assert attempts == 1


async def test_embedding_documents_retry_a_transient_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    embedder = build_embedder({"api_base": "https://embed.test/v1", "model": "embed"},
                              transport=httpx.MockTransport(handler))
    assert embedder is not None
    assert await embedder.embed_documents(["test"]) == [[1.0, 2.0]]
    assert attempts == 2


async def test_embedding_rejects_retry_after_beyond_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        pytest.fail("the embedder must not wait past its timeout")

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    embedder = build_embedder(
        {"api_base": "https://embed.test/v1", "model": "embed"},
        transport=httpx.MockTransport(lambda request: httpx.Response(503, headers={"Retry-After": "61"})),
    )
    assert embedder is not None
    with pytest.raises(httpx.HTTPStatusError):
        await embedder.embed_documents(["test"])


async def test_embedding_limits_parallel_batches() -> None:
    active = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        count = len(json.loads(request.content)["input"])
        return httpx.Response(
            200,
            json={"data": [{"index": index, "embedding": [1.0]} for index in range(count)]},
        )

    embedder = build_embedder(
        {"api_base": "https://embed.test/v1", "model": "embed"},
        transport=httpx.MockTransport(handler),
    )
    assert embedder is not None
    assert len(await embedder.embed_documents([str(index) for index in range(33)])) == 33
    assert peak == 4
