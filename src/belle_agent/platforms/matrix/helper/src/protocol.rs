//! The messages between Belle and the helper: one JSON object per line on fd 3.
//!
//! `protocol.jsonl` beside `Cargo.toml` holds one example of each message. The
//! Python side reads the same file, so a field that one side adds fails both suites.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::fmt;

/// Increments with any change of a message shape. Belle refuses any other value.
pub const PROTOCOL: u32 = 10;

/// A secret from Belle. Its `Debug` form never shows the value.
#[derive(Clone, Default, Deserialize, Serialize, PartialEq)]
#[serde(transparent)]
pub struct Secret(pub String);

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("<redacted>")
    }
}

impl Secret {
    pub fn given(&self) -> Option<&str> {
        Some(self.0.as_str()).filter(|s| !s.is_empty())
    }
}

/// One request from Belle. Each gets one answer with the same `id`.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum Op {
    Start {
        homeserver: String,
        user_id: String,
        store_dir: String,
        device_name: String,
        store_passphrase: Secret,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        password: Option<Secret>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        recovery_key: Option<Secret>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        recovery_key_output: Option<String>,
        /// An operator's identity reset: a new cross-signing identity and recovery key, written to
        /// `recovery_key_output`. Needs the password. The start then stops, as after a first start.
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        reset_identity: bool,
    },
    Send {
        room: String,
        #[serde(rename = "type")]
        event_type: String,
        content: Map<String, Value>,
        txn_id: String,
    },
    Ack {
        seq: u64,
    },
    Join {
        room: String,
    },
    Upload {
        room: String,
        mime: String,
        data: String,
    },
    /// Fetch a file by its plain `url` or its encrypted `file`, decrypted. A file over `max_bytes` is refused.
    Download {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        url: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        file: Option<Map<String, Value>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        max_bytes: Option<u64>,
    },
    /// Remove an event. `txn_id` makes a retry idempotent.
    Redact {
        room: String,
        event_id: String,
        txn_id: String,
    },
    /// Read one event, decrypted where the helper holds its key.
    Event {
        room: String,
        event_id: String,
    },
    Context {
        room: String,
        event_id: String,
        limit: u32,
    },
    /// What a joined room is: encrypted, public, direct, its joined members, its name and its alias.
    RoomInfo {
        room: String,
    },
    /// Resolve an alias without joining its room.
    AliasResolve {
        alias: String,
    },
    /// One page of a joined space's hierarchy.
    Hierarchy {
        room: String,
        limit: u32,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        from: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        max_depth: Option<u32>,
        #[serde(default)]
        suggested_only: bool,
    },
    /// Every joined room of the account, each as `room_info` gives it.
    Rooms {},
    /// The server's event search (`POST /search`). The server searches joined rooms it can read.
    /// `from` continues with a page's `next`.
    Search {
        search_term: String,
        limit: u32,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        from: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        keys: Option<Vec<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        rooms: Option<Vec<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        not_rooms: Option<Vec<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        senders: Option<Vec<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        not_senders: Option<Vec<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        types: Option<Vec<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        not_types: Option<Vec<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        order_by: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        before_limit: Option<u32>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        after_limit: Option<u32>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        include_profile: Option<bool>,
    },
    /// The server's user directory search (`POST /user_directory/search`).
    UserDirectory {
        search_term: String,
        limit: u32,
    },
    /// One page of the server's public room directory (`POST /publicRooms`), filtered by `search_term` where given.
    /// `from` continues with a page's `next`.
    PublicRooms {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        search_term: Option<String>,
        limit: u32,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        from: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        server: Option<String>,
    },
    /// One state event of a joined room, read from the server.
    StateGet {
        room: String,
        #[serde(rename = "type")]
        event_type: String,
        state_key: String,
    },
    /// Set one state event of a joined room. The server checks the account's power level.
    StatePut {
        room: String,
        #[serde(rename = "type")]
        event_type: String,
        state_key: String,
        content: Map<String, Value>,
    },
    /// Leave a joined room, or decline an invite.
    Leave {
        room: String,
    },
    /// Show the account as typing in a room for `timeout_ms`, or stop. `timeout_ms` is required with `typing: true`.
    Typing {
        room: String,
        typing: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        timeout_ms: Option<u64>,
    },
    /// Read a joined room's events, newest first, decrypted where the helper holds the key. `from` continues a
    /// page with its `next`. `before_event` starts just before that event. Neither: the newest events.
    Messages {
        room: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        from: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        before_event: Option<String>,
        limit: u32,
    },
    /// Read the events that relate to one event by `rel_type`, newest first: `m.thread` for the replies of a
    /// thread, `m.annotation` for the reactions. `from` continues a page with its `next`.
    Relations {
        room: String,
        event_id: String,
        rel_type: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        from: Option<String>,
        limit: u32,
    },
    /// The joined and invited members of a joined room.
    Members {
        room: String,
    },
    /// The joined direct room with a person, from the account's `m.direct`. Where none exists and `create` is
    /// true, a new encrypted direct room that invites them.
    DmRoom {
        user: String,
        create: bool,
    },
    /// The account's devices, as the server lists them. An operator's request.
    Devices {},
    /// Remove devices of the account, with the password for the server's user-interactive auth. The device of this
    /// store is refused. An operator's request.
    DeleteDevices {
        devices: Vec<String>,
        password: Secret,
    },
    /// Stop syncing, log out the device this start logged in as, and remove its store. Belle asks for it where it
    /// refuses the account after a start. A device that the store held before the start is refused: it is not the
    /// start's to undo.
    LogOut {},
    Shutdown {},
}

/// Every op name, in the order of `Op`.
pub const OPS: &[&str] = &[
    "start", "send", "ack", "join", "upload", "download", "redact", "event", "context", "room_info", "alias_resolve", "hierarchy", "rooms", "search",
    "user_directory", "public_rooms", "state_get", "state_put", "leave", "typing", "messages", "relations", "members",
    "dm_room", "devices", "delete_devices", "log_out", "shutdown",
];

/// What an ok answer carries, by op.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct StartResult {
    pub user_id: String,
    pub device_id: String,
    /// Whether this start logged in as a new device, rather than restoring the device of its store.
    pub new_device: bool,
    /// The SDK's backup state, such as `enabled`.
    pub backup: String,
    /// The server's `m.upload.size`. Absent where the server does not say.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_upload_bytes: Option<u64>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SendResult {
    pub event_id: String,
}

/// `url` in a room without encryption, `file` (an `EncryptedFile`) in an encrypted room.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct UploadResult {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub file: Option<Map<String, Value>>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct DownloadResult {
    pub data: String,
}

/// One event. `undecryptable`: it arrived encrypted and the helper holds no key, so `room_event` is the encrypted form.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EventResult {
    pub encrypted: bool,
    pub undecryptable: bool,
    pub room_event: Map<String, Value>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ContextResult {
    pub event: Option<EventResult>,
    pub before: Vec<EventResult>,
    pub after: Vec<EventResult>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub before_token: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub after_token: Option<String>,
}

/// One page of events. `next` reads the page after it, and is absent at the end.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PageResult {
    pub chunk: Vec<EventResult>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next: Option<String>,
}

/// `membership`: `join` or `invite`. `display_name` is absent where the member set none.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Member {
    pub user_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    pub membership: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MembersResult {
    pub members: Vec<Member>,
}

/// `room` is absent where no direct room exists and none was made.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct DmRoomResult {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub room: Option<String>,
    pub created: bool,
}

/// `public`: the join rule is `public`. `direct`: the account's `m.direct` names the room. `name` and `alias`:
/// the room's `m.room.name` and canonical alias, absent where the room has none.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RoomInfoResult {
    pub room: String,
    pub encrypted: bool,
    pub public: bool,
    pub direct: bool,
    pub joined_members: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub alias: Option<String>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RoomsResult {
    pub rooms: Vec<RoomInfoResult>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AliasResolveResult {
    pub room: String,
    pub servers: Vec<String>,
    pub joined: bool,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct HierarchyResult {
    pub rooms: Vec<Map<String, Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next: Option<String>,
}

/// One match of a search, as the server gives it. The event holds its `room_id`.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SearchHit {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rank: Option<f64>,
    pub room_event: Map<String, Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context: Option<Value>,
}

/// `count`: the server's estimate of every match. `next`: the page after this one, absent at the end.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SearchResult {
    pub chunk: Vec<SearchHit>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub count: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next: Option<String>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct DirectoryUser {
    pub user_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
}

/// `limited`: the server left out matches past the limit.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct UserDirectoryResult {
    pub results: Vec<DirectoryUser>,
    pub limited: bool,
}

/// Each room of the page as the server gives it (`PublicRoomsChunk`). `total`: the server's estimate of every room.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PublicRoomsResult {
    pub chunk: Vec<Map<String, Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total: Option<u64>,
}

/// `event`: the whole state event, with `sender` and `origin_server_ts` where the server gives them. Absent where the
/// room has none.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct StateGetResult {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event: Option<Map<String, Value>>,
}

/// One device of the account. `current`: the device of this store. `last_seen_ts`: milliseconds since the epoch.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Device {
    pub device_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_seen_ts: Option<u64>,
    pub current: bool,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct DevicesResult {
    pub devices: Vec<Device>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Empty {}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ErrorBody {
    /// A homeserver `errcode` as is, or a `helper_` code.
    pub code: String,
    pub message: String,
    /// Why a start was refused, for `helper_start_refused`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_after_ms: Option<u64>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(untagged)]
pub enum Answer {
    Ok { id: u64, ok: True, result: Value },
    Err { id: u64, ok: False, error: ErrorBody },
}

/// The literal `true` and `false` of an answer's `ok`.
#[derive(Debug, PartialEq)]
pub struct True;
#[derive(Debug, PartialEq)]
pub struct False;

macro_rules! literal_bool {
    ($ty:ident, $value:expr) => {
        impl Serialize for $ty {
            fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
                s.serialize_bool($value)
            }
        }
        impl<'de> Deserialize<'de> for $ty {
            fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                if bool::deserialize(d)? == $value {
                    Ok($ty)
                } else {
                    Err(serde::de::Error::custom(concat!("ok must be ", stringify!($value))))
                }
            }
        }
    };
}
literal_bool!(True, true);
literal_bool!(False, false);

/// What the helper reports on its own. Events carry no `id`.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub enum Event {
    /// The first line. The helper reads requests after it.
    Hello { protocol: u32, helper_version: String, sdk_version: String },
    /// One room event, decrypted where it arrived encrypted. Belle answers it with `ack`.
    /// `encrypted`: this event arrived encrypted. `direct`: the account's `m.direct` names the room.
    Timeline { seq: u64, room: String, encrypted: bool, direct: bool, room_event: Map<String, Value> },
    /// An encrypted event that the helper cannot decrypt. When its key arrives, the event follows as `timeline`.
    Utd { room: String, event_id: String, sender: String, session_id: String, direct: bool },
    /// An invitation of the account to a room. Belle answers it with `ack`, as a timeline event.
    Invite { seq: u64, room: String, inviter: String },
    /// A state that needs the operator. `store_rolled_back`: an old copy of the store. `bad_line`: a line that is no request.
    Alert { kind: String, message: String },
}

/// Every event name, in the order of `Event`.
#[cfg(test)]
pub const EVENTS: &[&str] = &["hello", "timeline", "utd", "invite", "alert"];

/// A line that is not a request this helper takes, with what the answer says.
#[derive(Debug, PartialEq)]
pub struct BadRequest {
    pub id: Option<u64>,
    pub message: String,
}

/// Read one request line. Refuse it with a message that names the field and the form it expects.
pub fn parse_request(line: &str) -> Result<(u64, Op), BadRequest> {
    let mut object = match serde_json::from_str::<Value>(line) {
        Ok(Value::Object(object)) => object,
        Ok(_) => return Err(BadRequest { id: None, message: "a request must be a JSON object".into() }),
        Err(e) => return Err(BadRequest { id: None, message: format!("a request must be JSON: {e}") }),
    };
    let id = match object.remove("id") {
        Some(Value::Number(n)) if n.is_u64() => n.as_u64().unwrap(),
        _ => return Err(BadRequest { id: None, message: "id must be a non-negative integer".into() }),
    };
    match object.get("op") {
        Some(Value::String(op)) if OPS.contains(&op.as_str()) => {}
        _ => return Err(BadRequest { id: Some(id), message: format!("op must be one of {}", OPS.join(", ")) }),
    }
    serde_json::from_value::<Op>(Value::Object(object))
        .map(|op| (id, op))
        .map_err(|e| BadRequest { id: Some(id), message: e.to_string() })
}

pub fn ok(id: u64, result: impl Serialize) -> Answer {
    Answer::Ok { id, ok: True, result: serde_json::to_value(result).expect("a result serializes") }
}

pub fn err(id: u64, code: &str, message: impl Into<String>) -> Answer {
    Answer::Err { id, ok: False, error: ErrorBody { code: code.into(), message: message.into(), reason: None, retry_after_ms: None } }
}

pub fn line(message: &impl Serialize) -> String {
    let mut text = serde_json::to_string(message).expect("a message serializes");
    text.push('\n');
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    const SCHEMA: &str = include_str!("../protocol.jsonl");

    fn lines() -> Vec<Value> {
        SCHEMA.lines().filter(|l| !l.trim().is_empty()).map(|l| serde_json::from_str(l).expect(l)).collect()
    }

    fn round_trip<T: serde::de::DeserializeOwned + Serialize>(value: &Value) {
        let typed: T = serde_json::from_value(value.clone()).unwrap_or_else(|e| panic!("{value}: {e}"));
        assert_eq!(&serde_json::to_value(&typed).unwrap(), value);
    }

    #[test]
    fn every_line_round_trips_and_every_message_has_a_line() {
        let mut ops = std::collections::HashMap::new();
        let mut events = vec![];
        for value in lines() {
            if let Some(op) = value.get("op") {
                let (id, typed) = parse_request(&value.to_string()).unwrap();
                let mut again = serde_json::to_value(&typed).unwrap();
                again.as_object_mut().unwrap().insert("id".into(), id.into());
                assert_eq!(again, value);
                ops.insert(id, op.as_str().unwrap().to_string());
            } else if let Some(event) = value.get("event") {
                round_trip::<Event>(&value);
                events.push(event.as_str().unwrap().to_string());
            } else {
                round_trip::<Answer>(&value);
                let id = value["id"].as_u64().unwrap();
                let op = ops.get(&id).unwrap_or_else(|| panic!("answer {id} follows no request"));
                if value["ok"] == true {
                    let result = &value["result"];
                    match op.as_str() {
                        "start" => round_trip::<StartResult>(result),
                        "send" | "redact" | "state_put" => round_trip::<SendResult>(result),
                        "rooms" => round_trip::<RoomsResult>(result),
                        "alias_resolve" => round_trip::<AliasResolveResult>(result),
                        "hierarchy" => round_trip::<HierarchyResult>(result),
                        "search" => round_trip::<SearchResult>(result),
                        "user_directory" => round_trip::<UserDirectoryResult>(result),
                        "public_rooms" => round_trip::<PublicRoomsResult>(result),
                        "state_get" => round_trip::<StateGetResult>(result),
                        "event" => round_trip::<EventResult>(result),
                        "context" => round_trip::<ContextResult>(result),
                        "room_info" => round_trip::<RoomInfoResult>(result),
                        "messages" | "relations" => round_trip::<PageResult>(result),
                        "members" => round_trip::<MembersResult>(result),
                        "dm_room" => round_trip::<DmRoomResult>(result),
                        "devices" => round_trip::<DevicesResult>(result),
                        "upload" => round_trip::<UploadResult>(result),
                        "download" => round_trip::<DownloadResult>(result),
                        _ => round_trip::<Empty>(result),
                    }
                }
            }
        }
        for name in OPS {
            assert!(ops.values().any(|o| o == name), "protocol.jsonl has no {name} request");
        }
        for name in EVENTS {
            assert!(events.iter().any(|e| e == name), "protocol.jsonl has no {name} event");
        }
    }

    #[test]
    fn an_unknown_field_is_refused_with_its_name() {
        let refused = parse_request(r#"{"id": 4, "op": "ack", "seq": 1, "extra": 2}"#).unwrap_err();
        assert_eq!(refused.id, Some(4));
        assert!(refused.message.contains("extra"), "{}", refused.message);
    }

    #[test]
    fn a_missing_field_is_refused_with_its_name() {
        let refused = parse_request(r#"{"id": 5, "op": "send", "room": "!r:s", "type": "m.room.message", "content": {}}"#).unwrap_err();
        assert!(refused.message.contains("txn_id"), "{}", refused.message);
    }

    #[test]
    fn a_request_without_an_id_or_with_an_unknown_op_is_refused() {
        assert_eq!(parse_request(r#"{"op": "ack", "seq": 1}"#).unwrap_err().id, None);
        let refused = parse_request(r#"{"id": 1, "op": "fly"}"#).unwrap_err();
        assert!(refused.message.contains("start, send"), "{}", refused.message);
        assert!(parse_request("not json").is_err());
    }

    #[test]
    fn a_secret_never_shows_in_debug_output() {
        let line = r#"{"id": 1, "op": "start", "homeserver": "http://h", "user_id": "@b:s", "store_dir": "/s",
            "device_name": "belle", "store_passphrase": "pass-AAA", "password": "pw-BBB", "recovery_key": "key-CCC"}"#;
        let (_, op) = parse_request(line).unwrap();
        let shown = format!("{op:?}");
        for secret in ["pass-AAA", "pw-BBB", "key-CCC"] {
            assert!(!shown.contains(secret), "{shown}");
        }
        let (_, op) = parse_request(r#"{"id": 2, "op": "delete_devices", "devices": ["OLD"], "password": "pw-DDD"}"#).unwrap();
        assert!(!format!("{op:?}").contains("pw-DDD"));
    }
}
