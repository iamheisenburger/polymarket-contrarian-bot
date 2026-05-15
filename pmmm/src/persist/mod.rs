//! Append-only JSONL audit log writer.
//!
//! Every decision, place, cancel, fill, and gate trigger writes a single
//! line of JSON. Matches the existing Python `DailyJsonlLogger` shape so
//! analysis scripts (`scripts/_postmortem_today.py` etc.) keep working.
//!
//! Two design choices:
//! 1. **Daily rotation** — file path is `prefix_YYYYMMDD.jsonl`. Auto-rotates
//!    at UTC midnight.
//! 2. **Best-effort, non-blocking** — log writer runs on a background tokio
//!    task with a bounded mpsc; if the channel fills (shouldn't happen with
//!    1024 buffer), we DROP the event rather than block the hot path.

use std::path::PathBuf;

use anyhow::{Context, Result};
use chrono::Utc;
use serde_json::Value;
use tokio::fs::{File, OpenOptions};
use tokio::io::{AsyncWriteExt, BufWriter};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tracing::{debug, warn};

use crate::types::now_unix_ns;

const CHANNEL_CAP: usize = 1024;

/// Handle to the background JSONL writer. Cheap to clone (`tx` is `Sender`).
#[derive(Clone)]
pub struct AuditLogger {
    tx: mpsc::Sender<String>,
}

pub struct AuditLoggerHandle {
    pub logger: AuditLogger,
    pub task: JoinHandle<()>,
}

impl AuditLogger {
    /// Spawn a background writer task that rotates daily and appends each
    /// queued line. Returns the logger handle + the task JoinHandle.
    pub fn spawn(prefix: impl Into<PathBuf>) -> AuditLoggerHandle {
        let prefix: PathBuf = prefix.into();
        let (tx, mut rx) = mpsc::channel::<String>(CHANNEL_CAP);

        let task = tokio::spawn(async move {
            let mut current_day: Option<String> = None;
            let mut writer: Option<BufWriter<File>> = None;
            while let Some(line) = rx.recv().await {
                let day = Utc::now().format("%Y%m%d").to_string();
                if current_day.as_deref() != Some(&day) {
                    // Rotate.
                    if let Some(mut w) = writer.take() {
                        let _ = w.flush().await;
                    }
                    let path = day_path(&prefix, &day);
                    match OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(&path)
                        .await
                    {
                        Ok(f) => {
                            writer = Some(BufWriter::new(f));
                            current_day = Some(day);
                            debug!(path = %path.display(), "audit log rotated");
                        }
                        Err(e) => {
                            warn!(error = %e, path = %path.display(), "open audit log");
                            continue;
                        }
                    }
                }
                if let Some(w) = writer.as_mut() {
                    if let Err(e) = w.write_all(line.as_bytes()).await {
                        warn!(error = %e, "write audit log");
                        continue;
                    }
                    let _ = w.write_all(b"\n").await;
                    // Flush every event — small cost, big win on crash safety.
                    let _ = w.flush().await;
                }
            }
        });

        AuditLoggerHandle {
            logger: AuditLogger { tx },
            task,
        }
    }

    /// Log one structured event. Non-blocking; drops on full channel.
    pub fn write(&self, event: &str, fields: Value) {
        let merged = match fields {
            Value::Object(mut m) => {
                m.insert("ts_ns".into(), Value::from(now_unix_ns()));
                m.insert("event".into(), Value::from(event));
                Value::Object(m)
            }
            other => serde_json::json!({"event": event, "ts_ns": now_unix_ns(), "data": other}),
        };
        let line = match serde_json::to_string(&merged) {
            Ok(s) => s,
            Err(_) => return,
        };
        if let Err(e) = self.tx.try_send(line) {
            warn!(error = %e, "audit log: dropped event");
        }
    }
}

fn day_path(prefix: &std::path::Path, day: &str) -> PathBuf {
    let mut p = prefix.to_path_buf();
    let stem = p
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "audit".to_string());
    p.set_file_name(format!("{}_{}.jsonl", stem, day));
    p
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn day_path_appends_yyyymmdd() {
        let p = day_path(std::path::Path::new("/tmp/audit"), "20260514");
        assert_eq!(p.to_str().unwrap(), "/tmp/audit_20260514.jsonl");
    }

    #[tokio::test]
    async fn write_and_read_one_event() {
        let dir = tempdir_in("/tmp");
        let prefix = dir.join("auditlog");
        let h = AuditLogger::spawn(prefix.clone());
        h.logger.write("hello", json!({"x": 1}));
        // Give the writer a moment to flush.
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        // Find the file (today's date).
        let today = Utc::now().format("%Y%m%d").to_string();
        let path = day_path(&prefix, &today);
        let content = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(content.contains("\"event\":\"hello\""), "got: {}", content);
        assert!(content.contains("\"x\":1"));
        // Cleanup task.
        drop(h.logger);
        let _ = tokio::time::timeout(std::time::Duration::from_secs(1), h.task).await;
        let _ = tokio::fs::remove_file(&path).await;
        let _ = tokio::fs::remove_dir(&dir).await;
    }

    fn tempdir_in(parent: &str) -> PathBuf {
        let dir = std::path::PathBuf::from(parent).join(format!("pmmm-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }
}
