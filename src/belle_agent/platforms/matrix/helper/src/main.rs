//! belle-matrix-helper: Belle's Matrix client.
//!
//! Belle starts it with one end of a socket pair as fd 3, and talks to it in the
//! JSON lines of `protocol.rs`. All Matrix traffic and all encryption run here,
//! in matrix-rust-sdk. The helper holds no Belle policy. It exits when the socket
//! closes, on `shutdown`, or on SIGTERM.

mod protocol;

use base64::{engine::general_purpose::STANDARD as B64, Engine};
use matrix_sdk::{
    authentication::matrix::MatrixSession,
    config::SyncSettings,
    encryption::{BackupDownloadStrategy, CrossSigningResetAuthType, EncryptionSettings},
    deserialized_responses::TimelineEvent,
    media::{MediaFormat, MediaRequestParameters},
    room::{IncludeRelations, MessagesOptions, RelationsOptions},
    ruma::{
        api::{client::{
            alias::get_alias,
            directory::get_public_rooms_filtered,
            membership::join_room_by_id,
            search::search_events,
            space::get_hierarchy,
            state::get_state_event_for_key,
            typing::create_typing_event::v3::{Request as TypingRequest, Typing, TypingInfo},
            uiaa,
        }, error::ErrorKind, Direction},
        events::{
            relation::RelationType,
            direct::DirectEventContent,
            room::{EncryptedFile, MediaSource},
            GlobalAccountDataEventType, StateEventType,
        },
        OwnedEventId, OwnedMxcUri, OwnedRoomId, OwnedServerName, OwnedTransactionId, OwnedUserId, RoomAliasId, RoomId,
    },
    Client, RoomMemberships, RoomState,
};
use protocol::*;
use serde_json::{json, Value};
use futures_util::StreamExt;
use std::{
    collections::{HashMap, HashSet},
    os::fd::FromRawFd,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::UnixStream,
    sync::{mpsc, watch, Notify, OnceCell},
    task::JoinHandle,
};
use tracing::{debug, error, info, warn};

const SDK_VERSION: &str = "0.19.1";
const SESSION_FILE: &str = "session.json";
/// The sync position up to which Belle acknowledged every event. A start hands Belle the events after it.
const TOKEN_FILE: &str = "belle-sync-token";
/// The file that one helper process holds locked while it uses the store.
const LOCK_FILE: &str = "belle-helper.lock";
const SYNC_TIMEOUT: Duration = Duration::from_secs(30);
/// The Megolm sessions whose undecryptable events wait for a key. Past it, a new session waits for none.
const MAX_WAITING_SESSIONS: usize = 1024;

/// An undecryptable event that waits for its key: its room, its id, and whether the room is direct.
type Waiting = (OwnedRoomId, OwnedEventId, bool);

/// What the request tasks share.
struct Helper {
    out: mpsc::UnboundedSender<String>,
    client: OnceCell<Client>,
    store_dir: OnceCell<PathBuf>,
    /// Whether the start logged in as a new device. Only such a device is the helper's to log out.
    new_device: OnceCell<bool>,
    /// The lock of the store, held while the process lives.
    store_lock: OnceCell<std::fs::File>,
    sync: Mutex<Option<JoinHandle<()>>>,
    /// The timeline events of the current sync batch that Belle has not acknowledged.
    pending: Mutex<HashSet<u64>>,
    /// The number of the last event handed to Belle.
    seq: AtomicU64,
    /// The undecryptable events of each Megolm session, by session id.
    undecrypted: Mutex<HashMap<String, Vec<Waiting>>>,
    acked: Notify,
    stop: watch::Sender<bool>,
}

impl Helper {
    fn send(&self, message: &impl serde::Serialize) {
        let _ = self.out.send(line(message));
    }

    async fn stopped(&self) {
        let _ = self.stop.subscribe().wait_for(|stop| *stop).await;
    }

    /// The number of a new event that Belle acknowledges, counted as pending.
    fn next_seq(&self) -> u64 {
        let seq = self.seq.fetch_add(1, Ordering::SeqCst) + 1;
        self.pending.lock().unwrap().insert(seq);
        seq
    }
}

/// A refused start: the reason, which Belle maps to the setting to fix, and a message.
struct Refusal(&'static str, String);

fn refuse(reason: &'static str, message: impl Into<String>) -> Refusal {
    Refusal(reason, message.into())
}

/// The reason of a start that the homeserver did not answer, or answered that it cannot serve now. A later start can
/// succeed, so Belle starts again, and the store of a new device is kept for it.
const UNREACHABLE: &str = "homeserver_unreachable";

/// Refuse with `reason`, or with `UNREACHABLE` where the homeserver did not answer.
fn refuse_sdk(reason: &'static str, e: &matrix_sdk::Error) -> Refusal {
    let unreachable = matches!(e, matrix_sdk::Error::Http(http) if http_unreachable(http));
    refuse(if unreachable { UNREACHABLE } else { reason }, e.to_string())
}

/// No answer at the network layer, a rate limit, or a server error: as the SDK itself judges a request to retry.
fn http_unreachable(e: &matrix_sdk::HttpError) -> bool {
    match e {
        matrix_sdk::HttpError::Reqwest(_) => true,
        matrix_sdk::HttpError::Cached(inner) => http_unreachable(inner),
        other => other.as_client_api_error().is_some_and(|m| m.status_code.as_u16() == 429 || m.status_code.is_server_error()),
    }
}

fn sdk_error(id: u64, e: &matrix_sdk::Error) -> Answer {
    api_error(id, e.as_client_api_error(), e.to_string())
}

/// A homeserver error passes its `errcode` and `retry_after_ms` through. Any other is `helper_failed`.
fn api_error(id: u64, server: Option<&matrix_sdk::ruma::api::error::Error>, text: String) -> Answer {
    let body = server.and_then(|e| match &e.body {
        matrix_sdk::ruma::api::error::ErrorBody::Standard(b) => serde_json::to_value(b).ok(),
        _ => None,
    });
    match body {
        Some(b) if b["errcode"].is_string() => Answer::Err {
            id,
            ok: False,
            error: ErrorBody {
                code: b["errcode"].as_str().unwrap_or_default().into(),
                message: b["error"].as_str().unwrap_or_default().into(),
                reason: None,
                retry_after_ms: b["retry_after_ms"].as_u64(),
            },
        },
        _ => err(id, "helper_failed", text),
    }
}

fn password_auth(client: &Client, password: &str, session: Option<String>) -> uiaa::AuthData {
    let user = client.user_id().expect("logged in").to_string();
    let mut auth = uiaa::Password::new(uiaa::UserIdentifier::Matrix(uiaa::MatrixUserIdentifier::new(user)), password.into());
    auth.session = session;
    uiaa::AuthData::Password(auth)
}

struct StartArgs<'a> {
    homeserver: &'a str,
    user_id: &'a str,
    store_dir: &'a Path,
    device_name: &'a str,
    passphrase: Option<&'a str>,
    password: Option<&'a str>,
    recovery_key: Option<&'a str>,
    recovery_key_output: Option<&'a str>,
    reset_identity: bool,
}

/// Lock the store for this process. Two processes on one store would use one device twice.
fn lock_store(store_dir: &Path) -> Result<std::fs::File, Refusal> {
    std::fs::create_dir_all(store_dir).map_err(|e| refuse("store_open_failed", format!("cannot create the store directory: {e}")))?;
    let file = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(store_dir.join(LOCK_FILE))
        .map_err(|e| refuse("store_open_failed", format!("cannot open the store's lock file: {e}")))?;
    match file.try_lock() {
        Ok(()) => Ok(file),
        Err(std::fs::TryLockError::WouldBlock) => Err(refuse("store_locked", "another helper process uses this store")),
        Err(std::fs::TryLockError::Error(e)) => Err(refuse("store_open_failed", format!("cannot lock the store: {e}"))),
    }
}

/// Open or create the account's session and crypto store, and return only with a cross-signed device.
async fn start(args: &StartArgs<'_>, out: &mpsc::UnboundedSender<String>) -> Result<(Client, StartResult), Refusal> {
    let session_file = args.store_dir.join(SESSION_FILE);
    let had_store = session_file.exists();
    let passphrase = args.passphrase.ok_or_else(|| refuse("store_passphrase_missing", "the store passphrase is empty"))?;
    if !had_store && args.password.is_none() {
        return Err(refuse("password_missing", "the store directory holds no session, and no password is given to log in"));
    }
    std::fs::create_dir_all(args.store_dir).map_err(|e| refuse("store_open_failed", format!("cannot create the store directory: {e}")))?;
    let client = Client::builder()
        .homeserver_url(args.homeserver)
        // Synapse names its public URL in the login answer. The helper keeps the URL that it was given.
        .respect_login_well_known(false)
        .sqlite_store(args.store_dir, Some(passphrase))
        .with_encryption_settings(EncryptionSettings {
            auto_enable_cross_signing: false,
            backup_download_strategy: BackupDownloadStrategy::AfterDecryptionFailure,
            auto_enable_backups: true,
        })
        .build()
        .await
        .map_err(|e| refuse("store_open_failed", format!("cannot open the store: {e}")))?;
    watch_duplicate_keys(&client, out.clone());
    if had_store {
        let text = std::fs::read_to_string(&session_file).map_err(|e| refuse("store_open_failed", e.to_string()))?;
        let session: MatrixSession = serde_json::from_str(&text).map_err(|e| refuse("store_open_failed", e.to_string()))?;
        client.restore_session(session).await.map_err(|e| refuse("store_open_failed", e.to_string()))?;
        info!(device = %client.device_id().map(|d| d.to_string()).unwrap_or_default(), "session restored");
    } else {
        client
            .matrix_auth()
            .login_username(args.user_id, args.password.unwrap_or_default())
            .initial_device_display_name(args.device_name)
            .send()
            .await
            .map_err(|e| refuse_sdk("login_failed", &e))?;
        let session = client.matrix_auth().session().ok_or_else(|| refuse("login_failed", "the login gave no session"))?;
        write_private(&session_file, &serde_json::to_string(&session).unwrap_or_default())
            .map_err(|e| refuse("store_open_failed", e.to_string()))?;
        info!(device = %client.device_id().map(|d| d.to_string()).unwrap_or_default(), "logged in as a new device");
    }
    match identify(&client, had_store, args).await {
        Ok(result) => Ok((client, result)),
        Err(refusal) => {
            if !had_store && !matches!(refusal.0, "bootstrapped" | "identity_reset" | UNREACHABLE) {
                // A new device that never became usable leaves no stale device and no store behind.
                if let Err(e) = client.matrix_auth().logout().await {
                    warn!("logout of the unusable device failed: {e}");
                }
                drop(client);
                remove_store(args.store_dir);
            }
            Err(refusal)
        }
    }
}

/// Undo a start that logged in as a new device, as a start that fails does: no device and no store stay behind.
async fn log_out(helper: &Helper, client: &Client, id: u64) -> Answer {
    // The sync loop writes the sync position into the store, so it stops before the store goes.
    if let Some(sync) = helper.sync.lock().unwrap().take() {
        sync.abort();
    }
    if let Err(e) = client.matrix_auth().logout().await {
        return api_error(id, e.as_client_api_error(), format!("log_out: {e}"));
    }
    if let Some(store_dir) = helper.store_dir.get() {
        remove_store(store_dir);
    }
    info!(device = %client.device_id().map(|d| d.to_string()).unwrap_or_default(), "logged out the new device");
    ok(id, Empty {})
}

fn remove_store(store_dir: &Path) {
    for entry in std::fs::read_dir(store_dir).into_iter().flatten().flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if name == SESSION_FILE || name == TOKEN_FILE || name.starts_with("matrix-sdk-") {
            let _ = std::fs::remove_file(entry.path());
        }
    }
}

async fn identify(client: &Client, had_store: bool, args: &StartArgs<'_>) -> Result<StartResult, Refusal> {
    let enc = client.encryption();
    let response = client
        .sync_once(SyncSettings::default().timeout(Duration::from_secs(0)))
        .await
        .map_err(|e| refuse_sdk("sync_failed", &e))?;
    debug!(events = timeline_count(&response), "first sync of this start");
    enc.wait_for_e2ee_initialization_tasks().await;
    let own = client.user_id().ok_or_else(|| refuse("login_failed", "no user id"))?.to_owned();
    let identity = enc.get_user_identity(&own).await.map_err(|e| refuse("sync_failed", e.to_string()))?;
    if identity.is_none() || args.reset_identity {
        let output = args.recovery_key_output.map(PathBuf::from).ok_or_else(|| {
            refuse("recovery_key_output_missing", "a new cross-signing identity needs a path for its new recovery key")
        })?;
        if output.exists() {
            return Err(refuse("recovery_key_output_exists", format!("the recovery key path {} exists already", output.display())));
        }
        if output.starts_with(args.store_dir) {
            return Err(refuse("recovery_key_output_in_store", "the recovery key path is inside the store directory"));
        }
        let password = args
            .password
            .ok_or_else(|| refuse("password_missing", "a new cross-signing identity needs the account password"))?;
        if identity.is_none() {
            if let Err(e) = enc.bootstrap_cross_signing(None).await {
                let Some(uiaa) = e.as_uiaa_response() else { return Err(refuse("bootstrap_failed", e.to_string())) };
                info!("the cross-signing upload needs user-interactive auth: using the password");
                let auth = password_auth(client, password, uiaa.session.clone());
                enc.bootstrap_cross_signing(Some(auth)).await.map_err(|e| refuse("bootstrap_failed", e.to_string()))?;
            }
        } else {
            reset_identity(client, password).await?;
        }
        let key = enc.recovery().enable().wait_for_backups_to_upload().await.map_err(|e| refuse("bootstrap_failed", e.to_string()))?;
        write_private(&output, &key).map_err(|e| refuse("bootstrap_failed", format!("cannot write the recovery key: {e}")))?;
        if identity.is_none() {
            info!("new cross-signing identity; recovery key written");
            return Err(refuse("bootstrapped", "a new identity was made: keep the recovery key as a secret, then start again"));
        }
        info!("identity reset; new recovery key written");
        return Err(refuse("identity_reset", "the identity was reset: keep the new recovery key as a secret, then start again"));
    }
    if !cross_signed(client).await {
        match args.recovery_key {
            Some(key) => enc.recovery().recover(key).await.map_err(|e| match &e {
                matrix_sdk::encryption::recovery::RecoveryError::Sdk(sdk) => refuse_sdk("recovery_failed", sdk),
                _ => refuse("recovery_failed", e.to_string()),
            })?,
            None if had_store => {
                return Err(refuse(
                    "not_cross_signed",
                    "this device is not cross-signed: the identity was reset elsewhere, or recovery never ran",
                ))
            }
            None => {
                return Err(refuse(
                    "recovery_key_missing",
                    "the account has a cross-signing identity and this new device has no recovery key",
                ))
            }
        }
    }
    if !wait_cross_signed(client).await {
        return Err(refuse("not_cross_signed", "this device is not cross-signed after recovery"));
    }
    if let Ok(devices) = enc.get_user_devices(&own).await {
        let others: Vec<String> = devices.devices().map(|d| d.device_id().to_string()).filter(|d| Some(d.as_str()) != client.device_id().map(|i| i.as_str())).collect();
        info!(count = others.len(), devices = ?others, "other devices of this account");
    }
    Ok(StartResult {
        user_id: own.to_string(),
        device_id: client.device_id().map(|d| d.to_string()).unwrap_or_default(),
        new_device: !had_store,
        backup: format!("{:?}", enc.backups().state()).to_lowercase(),
        max_upload_bytes: match client.load_or_fetch_max_upload_size().await {
            Ok(size) => Some(size.into()),
            Err(e) => {
                warn!("the server does not say its upload size limit: {e}");
                None
            }
        },
    })
}

/// Replace the account's cross-signing identity and delete its key backup, with the password for the server's
/// user-interactive auth. Every other device of the account loses its signature.
async fn reset_identity(client: &Client, password: &str) -> Result<(), Refusal> {
    let failed = |e: matrix_sdk::encryption::recovery::RecoveryError| refuse("reset_failed", e.to_string());
    let Some(handle) = client.encryption().recovery().reset_identity().await.map_err(failed)? else { return Ok(()) };
    match handle.auth_type() {
        CrossSigningResetAuthType::Uiaa(uiaa) => {
            info!("the identity reset needs user-interactive auth: using the password");
            handle.reset(Some(password_auth(client, password, uiaa.session.clone()))).await.map_err(failed)
        }
        CrossSigningResetAuthType::OAuth(_) => {
            handle.cancel().await;
            Err(refuse("reset_failed", "the server approves an identity reset in a browser (OAuth), which the helper cannot open"))
        }
    }
}

async fn cross_signed(client: &Client) -> bool {
    matches!(client.encryption().get_own_device().await, Ok(Some(d)) if d.is_cross_signed_by_owner())
}

/// A new signature of this device reaches the local store with the next key query, so sync for up to 10 s.
async fn wait_cross_signed(client: &Client) -> bool {
    for _ in 0..10 {
        if cross_signed(client).await {
            return true;
        }
        let _ = client.sync_once(SyncSettings::default().timeout(Duration::from_secs(0))).await;
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    cross_signed(client).await
}

/// Write a file that only this user can read. Refuse to replace one.
fn write_private(path: &Path, text: &str) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(path)?;
    file.write_all(text.as_bytes())
}

/// A store copied back from a snapshot uploads one-time keys that the server holds already.
fn watch_duplicate_keys(client: &Client, out: mpsc::UnboundedSender<String>) {
    let mut errors = client.subscribe_to_duplicate_key_upload_errors();
    tokio::spawn(async move {
        while errors.recv().await.is_ok() {
            error!("duplicate one-time key upload: this store is an old copy");
            let _ = out.send(line(&Event::Alert {
                kind: "store_rolled_back".into(),
                message: "the server refused a one-time key upload as a duplicate, so this store is an old copy. \
                          Discard the store, start as a new device and recover with the recovery key"
                    .into(),
            }));
        }
    });
}

fn read_token(store_dir: &Path) -> Option<String> {
    std::fs::read_to_string(store_dir.join(TOKEN_FILE)).ok().filter(|t| !t.is_empty())
}

fn write_token(store_dir: &Path, token: &str) -> std::io::Result<()> {
    let partial = store_dir.join(format!("{TOKEN_FILE}.partial"));
    std::fs::write(&partial, token)?;
    std::fs::rename(partial, store_dir.join(TOKEN_FILE))
}

fn timeline_count(response: &matrix_sdk::sync::SyncResponse) -> usize {
    response.rooms.joined.values().map(|room| room.timeline.events.len()).sum()
}

async fn send_invite(helper: &Helper, client: &Client, room_id: &RoomId) {
    let inviter = match client.get_room(room_id) {
        Some(room) => room.invite_details().await.map(|i| i.inviter_id.to_string()).unwrap_or_default(),
        None => String::new(),
    };
    helper.send(&Event::Invite { seq: helper.next_seq(), room: room_id.to_string(), inviter });
}

/// Whether the account's `m.direct` names the room.
async fn is_direct(room: &matrix_sdk::Room) -> bool {
    room.is_direct().await.unwrap_or(false)
}

/// Hand one room event to Belle: as `timeline`, which Belle acknowledges, or as `utd`.
///
/// `encrypted` says how this event arrived, not what the room is: Element sends reactions in clear in an
/// encrypted room.
/// An undecryptable event waits for its key, and goes to Belle as `timeline` once the key arrives.
fn emit(helper: &Helper, room_id: &RoomId, direct: bool, item: &TimelineEvent) {
    let encrypted = item.encryption_info().is_some();
    let Ok(Value::Object(event)) = item.raw().deserialize_as::<Value>() else { return };
    if item.kind.is_utd() {
        let field = |k: &str| event.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
        let session_id = event["content"]["session_id"].as_str().unwrap_or_default().to_string();
        wait_for_key(helper, &session_id, room_id, &field("event_id"), direct);
        helper.send(&Event::Utd { room: room_id.to_string(), event_id: field("event_id"), sender: field("sender"), session_id, direct });
        return;
    }
    helper.send(&Event::Timeline { seq: helper.next_seq(), room: room_id.to_string(), encrypted, direct, room_event: event });
}

fn wait_for_key(helper: &Helper, session_id: &str, room_id: &RoomId, event_id: &str, direct: bool) {
    let Ok(event_id) = OwnedEventId::try_from(event_id) else { return };
    let mut waiting = helper.undecrypted.lock().unwrap();
    if session_id.is_empty() || (waiting.len() >= MAX_WAITING_SESSIONS && !waiting.contains_key(session_id)) {
        return;
    }
    let events = waiting.entry(session_id.to_string()).or_default();
    if !events.iter().any(|(_, e, _)| *e == event_id) {
        events.push((room_id.to_owned(), event_id, direct));
    }
}

/// Hand Belle each undecryptable event whose room key arrived later, by key sharing or from the backup.
async fn late_decryptions(helper: Arc<Helper>, client: Client) {
    let Some(stream) = client.encryption().room_keys_received_stream().await else { return };
    tokio::pin!(stream);
    loop {
        let keys = tokio::select! { keys = stream.next() => keys, _ = helper.stopped() => return };
        let keys = match keys {
            Some(Ok(keys)) => keys,
            // The stream lagged: the keys it skipped decrypt the next event of their session.
            Some(Err(_)) => continue,
            None => return,
        };
        for key in keys {
            let waiting = helper.undecrypted.lock().unwrap().remove(&key.session_id).unwrap_or_default();
            for (room_id, event_id, direct) in waiting {
                let Some(room) = client.get_room(&room_id) else { continue };
                match room.event(&event_id, None).await {
                    Ok(item) if !item.kind.is_utd() => {
                        info!(event = %event_id, room = %room_id, "decrypted late");
                        emit(&helper, &room_id, direct, &item);
                    }
                    Ok(_) => wait_for_key(&helper, &key.session_id, &room_id, event_id.as_str(), direct),
                    Err(e) => warn!(event = %event_id, "cannot read an event whose key arrived: {e}"),
                }
            }
        }
    }
}

/// Wait until Belle has acknowledged every event handed so far. False when the helper stops first.
async fn all_acked(helper: &Helper) -> bool {
    loop {
        // Registered before the check, so an acknowledgement between the check and the wait is not missed.
        let acked = helper.acked.notified();
        tokio::pin!(acked);
        acked.as_mut().enable();
        if helper.pending.lock().unwrap().is_empty() {
            return true;
        }
        tokio::select! { _ = acked => {}, _ = helper.stopped() => return false }
    }
}

/// Hand Belle every room event after the acknowledged position, read with `/messages`.
///
/// The SDK saves its sync position before Belle has the events, and it drops a sync response that ends at
/// the position it saved. A sync from an older position therefore hands nothing again. `/messages`
/// does, decrypted. Belle may get an event twice, and drops the second by its event id.
async fn replay(helper: &Helper, client: &Client, since: &str) {
    let mut count = 0usize;
    for room in client.joined_rooms() {
        count += replay_room(helper, &room, since).await;
    }
    info!(events = count, "replayed the events after the acknowledged position");
}

/// Hand Belle the events of one room after `since`, read forward with `/messages`. Return their count.
async fn replay_room(helper: &Helper, room: &matrix_sdk::Room, since: &str) -> usize {
    let direct = is_direct(room).await;
    let mut count = 0usize;
    let mut from = since.to_string();
    loop {
        let mut options = MessagesOptions::forward().from(from.as_str());
        options.limit = 100u32.into();
        let page = match room.messages(options).await {
            Ok(page) => page,
            Err(e) => {
                warn!(room = %room.room_id(), "cannot read the events after {since}: {e}");
                return count;
            }
        };
        for item in &page.chunk {
            emit(helper, room.room_id(), direct, item);
        }
        count += page.chunk.len();
        match page.end {
            Some(end) if !page.chunk.is_empty() && end != from => from = end,
            _ => return count,
        }
    }
}

/// Sync, hand each batch to Belle, and save a position past a batch only once Belle acknowledged all of it.
async fn sync_loop(helper: Arc<Helper>) {
    let (Some(client), Some(store_dir)) = (helper.client.get().cloned(), helper.store_dir.get().cloned()) else { return };
    let mut delay = Duration::from_secs(1);
    tokio::spawn(late_decryptions(helper.clone(), client.clone()));
    match read_token(&store_dir) {
        Some(since) => replay(&helper, &client, &since).await,
        // A store's first start: the history before it is not handed to Belle.
        None => match client.sync_once(SyncSettings::default().timeout(Duration::from_secs(0))).await {
            Ok(response) => {
                if let Err(e) = write_token(&store_dir, &response.next_batch) {
                    error!("cannot save the sync position: {e}");
                }
            }
            Err(e) => warn!("first sync failed: {e}"),
        },
    }
    // Invites that wait from before this start, and those of a store's first sync, which no later sync repeats.
    for room in client.invited_rooms() {
        send_invite(&helper, &client, room.room_id()).await;
    }
    if !all_acked(&helper).await {
        return;
    }
    loop {
        let response = tokio::select! {
            r = client.sync_once(SyncSettings::default().timeout(SYNC_TIMEOUT)) => r,
            _ = helper.stopped() => return,
        };
        let response = match response {
            Ok(r) => {
                delay = Duration::from_secs(1);
                r
            }
            Err(e) => {
                warn!("sync failed: {e}; next attempt in {delay:?}");
                tokio::select! { _ = tokio::time::sleep(delay) => {}, _ = helper.stopped() => return }
                delay = (delay * 2).min(Duration::from_secs(60));
                continue;
            }
        };
        debug!(events = timeline_count(&response), "sync");
        for room_id in response.rooms.invited.keys() {
            send_invite(&helper, &client, room_id).await;
        }
        let since = read_token(&store_dir);
        for (room_id, update) in &response.rooms.joined {
            let Some(room) = client.get_room(room_id) else { continue };
            // A limited timeline skipped events. Read the room again from the acknowledged position.
            if let (true, Some(since)) = (update.timeline.limited, since.as_deref()) {
                let count = replay_room(&helper, &room, since).await;
                info!(room = %room_id, events = count, "filled a gap in the sync");
                continue;
            }
            let direct = is_direct(&room).await;
            for item in &update.timeline.events {
                emit(&helper, room_id, direct, item);
            }
        }
        // Backpressure: the next sync waits for every acknowledgement of this batch.
        if !all_acked(&helper).await {
            return;
        }
        if let Err(e) = write_token(&store_dir, &response.next_batch) {
            error!("cannot save the sync position: {e}");
        }
    }
}

async fn handle(helper: Arc<Helper>, id: u64, op: Op) {
    let answer = match op {
        Op::Start { homeserver, user_id, store_dir, device_name, store_passphrase, password, recovery_key, recovery_key_output, reset_identity } => {
            if helper.client.initialized() || helper.store_lock.initialized() {
                err(id, "helper_bad_request", "start: the helper has started already")
            } else {
                let store = PathBuf::from(&store_dir);
                let args = StartArgs {
                    homeserver: &homeserver,
                    user_id: &user_id,
                    store_dir: &store,
                    device_name: &device_name,
                    passphrase: store_passphrase.given(),
                    password: password.as_ref().and_then(Secret::given),
                    recovery_key: recovery_key.as_ref().and_then(Secret::given),
                    recovery_key_output: recovery_key_output.as_deref(),
                    reset_identity,
                };
                let started = match lock_store(&store) {
                    Ok(lock) => {
                        let _ = helper.store_lock.set(lock);
                        start(&args, &helper.out).await
                    }
                    Err(refusal) => Err(refusal),
                };
                match started {
                    Ok((client, result)) => {
                        let _ = helper.client.set(client);
                        let _ = helper.store_dir.set(store);
                        let _ = helper.new_device.set(result.new_device);
                        *helper.sync.lock().unwrap() = Some(tokio::spawn(sync_loop(helper.clone())));
                        ok(id, result)
                    }
                    Err(Refusal(reason, message)) => {
                        warn!(reason, "start refused: {message}");
                        Answer::Err {
                            id,
                            ok: False,
                            error: ErrorBody { code: "helper_start_refused".into(), message, reason: Some(reason.into()), retry_after_ms: None },
                        }
                    }
                }
            }
        }
        Op::Ack { seq } => {
            helper.pending.lock().unwrap().remove(&seq);
            helper.acked.notify_waiters();
            ok(id, Empty {})
        }
        Op::Shutdown {} => ok(id, Empty {}),
        Op::LogOut {} => match helper.client.get() {
            None => err(id, "helper_not_ready", "the helper has not started: send start first"),
            Some(_) if helper.new_device.get() != Some(&true) => {
                err(id, "helper_bad_request", "log_out: the store held this device before the start, so it stays")
            }
            Some(client) => log_out(&helper, client, id).await,
        },
        other => match helper.client.get() {
            None => err(id, "helper_not_ready", "the helper has not started: send start first"),
            Some(client) => room_op(client, id, other).await,
        },
    };
    helper.send(&answer);
}

/// Add a room to the account's `m.direct`, read from the server, not from the sync cache.
async fn mark_direct(client: &Client, room: &OwnedRoomId, user: &OwnedUserId) -> matrix_sdk::Result<()> {
    let current = client.account().fetch_account_data(GlobalAccountDataEventType::Direct).await?;
    let mut content: DirectEventContent = match current {
        Some(raw) => serde_json::from_str(raw.json().get())?,
        None => DirectEventContent::default(),
    };
    let rooms = content.entry(user.clone().into()).or_default();
    if !rooms.contains(room) {
        rooms.push(room.clone());
        client.account().set_account_data(content).await?;
    }
    Ok(())
}

fn room_id(id: u64, room: &str) -> Result<OwnedRoomId, Answer> {
    OwnedRoomId::try_from(room).map_err(|_| err(id, "helper_bad_request", format!("room must be a room id such as !abc:server, not {room:?}")))
}

fn event_id_of(id: u64, event_id: &str) -> Result<OwnedEventId, Answer> {
    OwnedEventId::try_from(event_id)
        .map_err(|_| err(id, "helper_bad_request", format!("event_id must be an event id such as $abc, not {event_id:?}")))
}

/// One event as an answer gives it. ``None`` for an event that is no JSON object.
fn event_result(item: &TimelineEvent) -> Option<EventResult> {
    match item.raw().deserialize_as::<Value>() {
        Ok(Value::Object(room_event)) => Some(EventResult {
            encrypted: item.encryption_info().is_some() || item.kind.is_utd(),
            undecryptable: item.kind.is_utd(),
            room_event,
        }),
        _ => None,
    }
}

fn page(chunk: &[TimelineEvent], next: Option<String>, from: Option<&String>) -> PageResult {
    // A server can answer the end of a room with the token it was given. That is no further page.
    PageResult { chunk: chunk.iter().filter_map(event_result).collect(), next: next.filter(|n| Some(n) != from) }
}

/// What a joined room is. An unknown encryption state counts as encrypted: its size budget is the smaller one.
async fn room_info(room: &matrix_sdk::Room) -> RoomInfoResult {
    RoomInfoResult {
        room: room.room_id().to_string(),
        encrypted: room.latest_encryption_state().await.map(|s| s.is_encrypted()).unwrap_or(true),
        public: room.is_public().unwrap_or(false),
        direct: is_direct(room).await,
        joined_members: room.joined_members_count(),
        name: room.name(),
        alias: room.canonical_alias().map(|alias| alias.to_string()),
    }
}

fn joined(client: &Client, id: u64, room: &str) -> Result<matrix_sdk::Room, Answer> {
    let room_id = room_id(id, room)?;
    match client.get_room(&room_id) {
        Some(r) if r.state() == RoomState::Joined => Ok(r),
        _ => Err(err(id, "helper_not_joined", format!("room {room} is not a joined room of this account"))),
    }
}

async fn room_op(client: &Client, id: u64, op: Op) -> Answer {
    let result: Result<Answer, Answer> = async {
        Ok(match op {
            Op::Send { room, event_type, content, txn_id } => {
                let room = joined(client, id, &room)?;
                match room.send_raw(&event_type, Value::Object(content)).with_transaction_id(&OwnedTransactionId::from(txn_id)).await {
                    Ok(r) => ok(id, SendResult { event_id: r.response.event_id.to_string() }),
                    Err(e) => sdk_error(id, &e),
                }
            }
            Op::Join { room } => {
                let room_id = room_id(id, &room)?;
                let direct_with = match client.get_room(&room_id) {
                    Some(r) if r.state() == RoomState::Invited && r.is_direct().await.unwrap_or(false) => {
                        r.invite_details().await.ok().map(|i| i.inviter_id)
                    }
                    _ => None,
                };
                // The plain request: the SDK's own join marks a direct room from its cached m.direct, which is
                // stale while the sync waits for Belle, so several joins in one batch keep only the last one.
                if let Err(e) = client.send(join_room_by_id::v3::Request::new(room_id.clone())).await {
                    return Err(api_error(id, e.as_client_api_error(), e.to_string()));
                }
                if let Some(user) = direct_with {
                    if let Err(e) = mark_direct(client, &room_id, &user).await {
                        warn!("joined {room_id}, but cannot add it to m.direct: {e}");
                    }
                }
                ok(id, Empty {})
            }
            Op::Upload { room, mime, data } => {
                let room = joined(client, id, &room)?;
                let data = B64.decode(data).map_err(|e| err(id, "helper_bad_request", format!("data must be standard base64: {e}")))?;
                let mime: mime::Mime =
                    mime.parse().map_err(|_| err(id, "helper_bad_request", format!("mime must be a media type such as text/plain, not {mime:?}")))?;
                // An unknown encryption state counts as encrypted: never upload plaintext by mistake.
                if room.latest_encryption_state().await.map(|s| s.is_encrypted()).unwrap_or(true) {
                    let mut reader = std::io::Cursor::new(data);
                    match client.upload_encrypted_file(&mut reader).await {
                        Ok(file) => ok(id, UploadResult { url: None, file: serde_json::to_value(file).ok().and_then(|v| v.as_object().cloned()) }),
                        Err(e) => sdk_error(id, &e),
                    }
                } else {
                    match client.media().upload(&mime, data, None).await {
                        Ok(r) => ok(id, UploadResult { url: Some(r.content_uri.to_string()), file: None }),
                        Err(e) => sdk_error(id, &e),
                    }
                }
            }
            Op::Download { url, file, max_bytes } => {
                let source = match (url, file) {
                    (Some(url), None) => MediaSource::Plain(OwnedMxcUri::from(url)),
                    (None, Some(file)) => MediaSource::Encrypted(Box::new(
                        serde_json::from_value::<EncryptedFile>(Value::Object(file))
                            .map_err(|e| err(id, "helper_bad_request", format!("file must be an EncryptedFile object: {e}")))?,
                    )),
                    _ => return Err(err(id, "helper_bad_request", "download takes exactly one of url and file")),
                };
                let request = MediaRequestParameters { source, format: MediaFormat::File };
                match client.media().get_media_content(&request, false).await {
                    // The SDK reads the whole file. The limit keeps it off the line, which has a limit of its own.
                    Ok(bytes) if max_bytes.is_some_and(|max| bytes.len() as u64 > max) => err(
                        id,
                        "helper_too_large",
                        format!("the file is {} bytes, over the limit of {} bytes", bytes.len(), max_bytes.unwrap_or_default()),
                    ),
                    Ok(bytes) => ok(id, DownloadResult { data: B64.encode(bytes) }),
                    Err(e) => sdk_error(id, &e),
                }
            }
            Op::Redact { room, event_id, txn_id } => {
                let room = joined(client, id, &room)?;
                let event_id = event_id_of(id, &event_id)?;
                match room.redact(&event_id, None, Some(OwnedTransactionId::from(txn_id))).await {
                    Ok(r) => ok(id, SendResult { event_id: r.event_id.to_string() }),
                    Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
                }
            }
            Op::Event { room, event_id } => {
                let room = joined(client, id, &room)?;
                let event_id = event_id_of(id, &event_id)?;
                match room.event(&event_id, None).await {
                    Ok(item) => match event_result(&item) {
                        Some(result) => ok(id, result),
                        None => err(id, "helper_failed", format!("event {event_id} is not a JSON object")),
                    },
                    Err(e) => sdk_error(id, &e),
                }
            }
            Op::Context { room, event_id, limit } => {
                let room = joined(client, id, &room)?;
                let event_id = event_id_of(id, &event_id)?;
                match room.event_with_context(&event_id, false, limit.into(), None).await {
                    Ok(found) => ok(id, ContextResult {
                        event: found.event.as_ref().and_then(event_result),
                        before: found.events_before.iter().filter_map(event_result).collect(),
                        after: found.events_after.iter().filter_map(event_result).collect(),
                        before_token: found.prev_batch_token,
                        after_token: found.next_batch_token,
                    }),
                    Err(e) => sdk_error(id, &e),
                }
            }
            Op::RoomInfo { room } => ok(id, room_info(&joined(client, id, &room)?).await),
            Op::AliasResolve { alias } => {
                let alias_id = RoomAliasId::parse(&alias)
                    .map_err(|e| err(id, "helper_bad_request", format!("alias is not a room alias: {e}")))?;
                match client.send(get_alias::v3::Request::new(alias_id)).await {
                    Ok(found) => ok(id, AliasResolveResult {
                        joined: client.get_room(&found.room_id).is_some_and(|room| room.state() == RoomState::Joined),
                        room: found.room_id.to_string(),
                        servers: found.servers.into_iter().map(|server| server.to_string()).collect(),
                    }),
                    Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
                }
            }
            Op::Hierarchy { room, limit, from, max_depth, suggested_only } => {
                let room = joined(client, id, &room)?;
                let mut request = get_hierarchy::v1::Request::new(room.room_id().to_owned());
                request.from = from;
                request.limit = Some(limit.into());
                request.max_depth = max_depth.map(Into::into);
                request.suggested_only = suggested_only;
                match client.send(request).await {
                    Ok(found) => {
                        let rooms = found.rooms.into_iter().map(|entry| {
                            let value = serde_json::to_value(entry)
                                .map_err(|e| err(id, "helper_failed", format!("cannot encode space room: {e}")))?;
                            value.as_object().cloned()
                                .ok_or_else(|| err(id, "helper_failed", "space room is not a JSON object"))
                        }).collect::<Result<Vec<_>, _>>()?;
                        ok(id, HierarchyResult { rooms, next: found.next_batch })
                    }
                    Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
                }
            }
            Op::Rooms {} => {
                let mut rooms = vec![];
                for room in client.joined_rooms() {
                    rooms.push(room_info(&room).await);
                }
                ok(id, RoomsResult { rooms })
            }
            Op::Search {
                search_term, limit, from, keys, rooms, not_rooms, senders, not_senders,
                types, not_types, order_by, before_limit, after_limit, include_profile,
            } => {
                let mut criteria = search_events::v3::Criteria::new(search_term);
                criteria.order_by = Some(search_events::v3::OrderBy::Recent);
                criteria.filter.limit = Some(limit.into());
                let mut value = serde_json::to_value(&criteria)
                    .map_err(|e| err(id, "helper_bad_request", format!("search criteria: {e}")))?;
                if let Some(keys) = keys { value["keys"] = json!(keys); }
                if let Some(rooms) = rooms { value["filter"]["rooms"] = json!(rooms); }
                if let Some(not_rooms) = not_rooms { value["filter"]["not_rooms"] = json!(not_rooms); }
                if let Some(senders) = senders { value["filter"]["senders"] = json!(senders); }
                if let Some(not_senders) = not_senders { value["filter"]["not_senders"] = json!(not_senders); }
                if let Some(types) = types { value["filter"]["types"] = json!(types); }
                if let Some(not_types) = not_types { value["filter"]["not_types"] = json!(not_types); }
                if let Some(order_by) = order_by { value["order_by"] = json!(order_by); }
                if before_limit.is_some() || after_limit.is_some() || include_profile.is_some() {
                    value["event_context"] = json!({
                        "before_limit": before_limit.unwrap_or(0),
                        "after_limit": after_limit.unwrap_or(0),
                        "include_profile": include_profile.unwrap_or(false),
                    });
                }
                criteria = serde_json::from_value(value)
                    .map_err(|e| err(id, "helper_bad_request", format!("search criteria: {e}")))?;
                let mut categories = search_events::v3::Categories::new();
                categories.room_events = Some(criteria);
                let mut request = search_events::v3::Request::new(categories);
                request.next_batch = from;
                match client.send(request).await {
                    Ok(found) => {
                        let found = found.search_categories.room_events;
                        let chunk = found
                            .results
                            .into_iter()
                            .filter_map(|hit| match hit.result.map(|raw| raw.deserialize_as::<Value>()) {
                                Some(Ok(Value::Object(room_event))) => Some(SearchHit {
                                    rank: hit.rank, room_event,
                                    context: serde_json::to_value(&hit.context).ok().filter(|value| !value.is_null()),
                                }),
                                _ => None,
                            })
                            .collect();
                        ok(id, SearchResult { chunk, count: found.count.map(u64::from), next: found.next_batch })
                    }
                    Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
                }
            }
            Op::UserDirectory { search_term, limit } => match client.search_users(&search_term, limit.into()).await {
                Ok(found) => ok(
                    id,
                    UserDirectoryResult {
                        results: found
                            .results
                            .into_iter()
                            .map(|user| DirectoryUser { user_id: user.user_id.to_string(), display_name: user.display_name })
                            .collect(),
                        limited: found.limited,
                    },
                ),
                Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
            },
            Op::PublicRooms { search_term, limit, from, server } => {
                let mut request = get_public_rooms_filtered::v3::Request::new();
                request.limit = Some(limit.into());
                request.since = from;
                request.filter.generic_search_term = search_term;
                request.server = match server {
                    Some(name) => Some(OwnedServerName::try_from(name.as_str())
                        .map_err(|_| err(id, "helper_bad_request", "server must be a Matrix server name"))?),
                    None => None,
                };
                match client.public_rooms_filtered(request).await {
                    Ok(found) => ok(
                        id,
                        PublicRoomsResult {
                            chunk: found
                                .chunk
                                .iter()
                                .filter_map(|room| match serde_json::to_value(room) {
                                    Ok(Value::Object(room)) => Some(room),
                                    _ => None,
                                })
                                .collect(),
                            next: found.next_batch,
                            total: found.total_room_count_estimate.map(u64::from),
                        },
                    ),
                    Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
                }
            }
            Op::StateGet { room, event_type, state_key } => {
                let room = joined(client, id, &room)?;
                // From the server, not the store: the store lags behind a change that was just made.
                let mut request = get_state_event_for_key::v3::Request::new(
                    room.room_id().to_owned(),
                    StateEventType::from(event_type.as_str()),
                    state_key,
                );
                request.format = get_state_event_for_key::v3::StateEventFormat::Event;
                match client.send(request).await {
                    Ok(found) => match serde_json::from_str::<Value>(found.event_or_content.get()) {
                        // A server that does not know the format gives the content alone.
                        Ok(Value::Object(event)) if event.get("content").is_some_and(Value::is_object) && event.contains_key("type") => {
                            ok(id, StateGetResult { event: Some(event) })
                        }
                        Ok(Value::Object(content)) => {
                            let mut event = serde_json::Map::new();
                            event.insert("type".into(), Value::String(event_type));
                            event.insert("content".into(), Value::Object(content));
                            ok(id, StateGetResult { event: Some(event) })
                        }
                        _ => err(id, "helper_failed", format!("the state event {event_type} is not a JSON object")),
                    },
                    Err(e) if e.client_api_error_kind() == Some(&ErrorKind::NotFound) => ok(id, StateGetResult { event: None }),
                    Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
                }
            }
            Op::StatePut { room, event_type, state_key, content } => {
                let room = joined(client, id, &room)?;
                match room.send_state_event_raw(&event_type, &state_key, Value::Object(content)).await {
                    Ok(r) => ok(id, SendResult { event_id: r.event_id.to_string() }),
                    Err(e) => sdk_error(id, &e),
                }
            }
            Op::Leave { room } => {
                let room_id = room_id(id, &room)?;
                let Some(room) = client.get_room(&room_id) else {
                    return Err(err(id, "helper_not_joined", format!("room {room_id} is neither joined nor an invite of this account")));
                };
                match room.leave().await {
                    Ok(()) => ok(id, Empty {}),
                    Err(e) => sdk_error(id, &e),
                }
            }
            Op::Typing { room, typing, timeout_ms } => {
                let room = joined(client, id, &room)?;
                let state = match (typing, timeout_ms) {
                    (false, _) => Typing::No,
                    (true, Some(ms)) => Typing::Yes(TypingInfo::new(Duration::from_millis(ms))),
                    (true, None) => return Err(err(id, "helper_bad_request", "typing: timeout_ms is required with typing: true")),
                };
                let user = client.user_id().expect("started").to_owned();
                match client.send(TypingRequest::new(user, room.room_id().to_owned(), state)).await {
                    Ok(_) => ok(id, Empty {}),
                    Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
                }
            }
            Op::Messages { room, from, before_event, limit } => {
                let room = joined(client, id, &room)?;
                let from = match (from, before_event) {
                    (Some(_), Some(_)) => return Err(err(id, "helper_bad_request", "messages takes at most one of from and before_event")),
                    (None, Some(event)) => {
                        let event_id = event_id_of(id, &event)?;
                        match room.event_with_context(&event_id, false, 0u32.into(), None).await {
                            Ok(context) => match context.prev_batch_token {
                                Some(token) => Some(token),
                                None => return Ok(ok(id, PageResult { chunk: vec![], next: None })),
                            },
                            Err(e) => return Err(sdk_error(id, &e)),
                        }
                    }
                    (from, None) => from,
                };
                let mut options = MessagesOptions::backward();
                options.from = from.clone();
                options.limit = limit.into();
                match room.messages(options).await {
                    Ok(found) => ok(id, page(&found.chunk, found.end, from.as_ref())),
                    Err(e) => sdk_error(id, &e),
                }
            }
            Op::Relations { room, event_id, rel_type, from, limit } => {
                let room = joined(client, id, &room)?;
                let event_id = event_id_of(id, &event_id)?;
                let options = RelationsOptions {
                    from: from.clone(),
                    dir: Direction::Backward,
                    limit: Some(limit.into()),
                    include_relations: IncludeRelations::RelationsOfType(RelationType::from(rel_type.as_str())),
                    recurse: false,
                };
                match room.relations(event_id, options).await {
                    Ok(found) => ok(id, page(&found.chunk, found.next_batch_token, from.as_ref())),
                    Err(e) => sdk_error(id, &e),
                }
            }
            Op::Members { room } => {
                let room = joined(client, id, &room)?;
                match room.members(RoomMemberships::JOIN | RoomMemberships::INVITE).await {
                    Ok(members) => ok(
                        id,
                        MembersResult {
                            members: members
                                .iter()
                                .map(|m| Member {
                                    user_id: m.user_id().to_string(),
                                    display_name: m.display_name().map(str::to_owned),
                                    membership: m.membership().to_string(),
                                })
                                .collect(),
                        },
                    ),
                    Err(e) => sdk_error(id, &e),
                }
            }
            Op::DmRoom { user, create } => {
                let user = OwnedUserId::try_from(user.as_str())
                    .map_err(|_| err(id, "helper_bad_request", format!("user must be a user id such as @alice:server, not {user:?}")))?;
                match client.get_dm_room(&user) {
                    Some(room) => ok(id, DmRoomResult { room: Some(room.room_id().to_string()), created: false }),
                    None if !create => ok(id, DmRoomResult { room: None, created: false }),
                    None => match client.create_dm(&user).await {
                        Ok(room) => ok(id, DmRoomResult { room: Some(room.room_id().to_string()), created: true }),
                        Err(e) => sdk_error(id, &e),
                    },
                }
            }
            Op::Devices {} => match client.devices().await {
                Ok(response) => {
                    let current = client.device_id();
                    let devices = response
                        .devices
                        .into_iter()
                        .map(|d| Device {
                            current: Some(d.device_id.as_ref()) == current,
                            device_id: d.device_id.to_string(),
                            display_name: d.display_name,
                            last_seen_ts: d.last_seen_ts.map(|t| t.0.into()),
                        })
                        .collect();
                    ok(id, DevicesResult { devices })
                }
                Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
            },
            Op::DeleteDevices { devices, password } => {
                let mut ids = Vec::new();
                for device in &devices {
                    let device_id = matrix_sdk::ruma::OwnedDeviceId::from(device.as_str());
                    if Some(device_id.as_ref()) == client.device_id() {
                        return Err(err(id, "helper_bad_request", format!("devices: {device} is the device of this store")));
                    }
                    ids.push(device_id);
                }
                let Some(password) = password.given() else {
                    return Err(err(id, "helper_bad_request", "password must be the account password, not empty"));
                };
                let answer = match client.delete_devices(&ids, None).await {
                    Err(e) if e.as_uiaa_response().is_some() => {
                        info!(count = ids.len(), "the device removal needs user-interactive auth: using the password");
                        let session = e.as_uiaa_response().and_then(|u| u.session.clone());
                        client.delete_devices(&ids, Some(password_auth(client, password, session))).await
                    }
                    other => other,
                };
                match answer {
                    Ok(_) => {
                        info!(devices = ?devices, "devices removed");
                        ok(id, Empty {})
                    }
                    Err(e) => api_error(id, e.as_client_api_error(), e.to_string()),
                }
            }
            Op::Start { .. } | Op::Ack { .. } | Op::LogOut {} | Op::Shutdown {} => err(id, "helper_bad_request", "not a room operation"),
        })
    }
    .await;
    result.unwrap_or_else(|answer| answer)
}

fn max_line_bytes() -> Result<usize, String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [flag, value] if flag == "--max-line-bytes" => value.parse().map_err(|_| format!("--max-line-bytes must be a byte count, not {value:?}")),
        _ => Err("usage: belle-matrix-helper --max-line-bytes <bytes>, with the socket to Belle as fd 3".into()),
    }
}

/// Read requests until the socket closes or a shutdown arrives, and say which.
async fn read_requests(helper: Arc<Helper>, read: tokio::net::unix::OwnedReadHalf, limit: usize) -> &'static str {
    let mut read = BufReader::new(read);
    let mut buf = Vec::new();
    loop {
        buf.clear();
        match (&mut read).take(limit as u64 + 1).read_until(b'\n', &mut buf).await {
            Ok(0) | Err(_) => return "the socket to Belle closed",
            Ok(_) if buf.len() > limit => return "a line from Belle is longer than --max-line-bytes",
            Ok(_) => {}
        }
        match parse_request(String::from_utf8_lossy(&buf).trim_end()) {
            Ok((id, Op::Shutdown {})) => {
                handle(helper.clone(), id, Op::Shutdown {}).await;
                return "shutdown";
            }
            Ok((id, op)) => {
                tokio::spawn(handle(helper.clone(), id, op));
            }
            Err(BadRequest { id: Some(id), message }) => helper.send(&err(id, "helper_bad_request", message)),
            Err(BadRequest { id: None, message }) => helper.send(&Event::Alert { kind: "bad_line".into(), message }),
        }
    }
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_ansi(false)
        .with_env_filter(std::env::var("BELLE_MATRIX_HELPER_LOG").unwrap_or_else(|_| "warn,matrix_sdk::http_client=off,belle_matrix_helper=info".into()))
        .init();
    let limit = match max_line_bytes() {
        Ok(limit) => limit,
        Err(message) => {
            eprintln!("{message}");
            std::process::exit(2);
        }
    };
    // SAFETY: Belle passes the socket as fd 3, and nothing else in this process owns fd 3.
    let socket = unsafe { std::os::unix::net::UnixStream::from_raw_fd(3) };
    let socket = match socket.set_nonblocking(true).and_then(|_| UnixStream::from_std(socket)) {
        Ok(socket) => socket,
        Err(e) => {
            eprintln!("fd 3 is not the socket to Belle: {e}");
            std::process::exit(2);
        }
    };
    let (read, mut write) = socket.into_split();
    let (out, mut lines) = mpsc::unbounded_channel::<String>();
    let writer = tokio::spawn(async move {
        while let Some(text) = lines.recv().await {
            if write.write_all(text.as_bytes()).await.is_err() {
                break;
            }
        }
    });
    let helper = Arc::new(Helper {
        out,
        client: OnceCell::new(),
        store_dir: OnceCell::new(),
        new_device: OnceCell::new(),
        store_lock: OnceCell::new(),
        sync: Mutex::new(None),
        pending: Mutex::new(HashSet::new()),
        seq: AtomicU64::new(0),
        undecrypted: Mutex::new(HashMap::new()),
        acked: Notify::new(),
        stop: watch::channel(false).0,
    });
    helper.send(&Event::Hello { protocol: PROTOCOL, helper_version: env!("CARGO_PKG_VERSION").into(), sdk_version: SDK_VERSION.into() });

    let mut term = match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
        Ok(term) => term,
        Err(e) => {
            eprintln!("cannot handle SIGTERM: {e}");
            std::process::exit(2);
        }
    };
    let why = tokio::select! {
        why = read_requests(helper.clone(), read, limit) => why,
        _ = term.recv() => "SIGTERM",
    };
    info!("stopping: {why}");
    let _ = helper.stop.send(true);
    // The sync loop ends at its next await. The SDK's store writes run to their end on blocking threads.
    let sync = helper.sync.lock().unwrap().take();
    if let Some(sync) = sync {
        let _ = tokio::time::timeout(Duration::from_secs(5), sync).await;
    }
    // Let the answer to shutdown reach the socket.
    tokio::time::sleep(Duration::from_millis(50)).await;
    writer.abort();
    info!("stopped");
}

#[cfg(test)]
mod tests {
    use super::http_unreachable;
    use matrix_sdk::ruma::api::client::uiaa::UiaaResponse;
    use matrix_sdk::ruma::api::error::{Error, ErrorBody, FromHttpResponseError};
    use matrix_sdk::ruma::exports::http::StatusCode;
    use matrix_sdk::HttpError;

    fn answered(status: u16) -> HttpError {
        let error = Error::new(StatusCode::from_u16(status).unwrap(), ErrorBody::Json(serde_json::json!({"errcode": "M_UNKNOWN"})));
        HttpError::Api(Box::new(FromHttpResponseError::Server(UiaaResponse::MatrixError(error))))
    }

    #[test]
    fn a_rate_limit_or_a_server_error_is_no_answer_and_a_refusal_is_one() {
        assert!(http_unreachable(&answered(502)));
        assert!(http_unreachable(&answered(429)));
        assert!(http_unreachable(&HttpError::Cached(std::sync::Arc::new(answered(503)))));
        assert!(!http_unreachable(&answered(403)));
        assert!(!http_unreachable(&answered(401)));
    }
}
