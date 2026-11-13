"""Usage logging for reporting: scrubbed, non-identifying events pushed to a private HF dataset.

Each event is one JSON line in <local>/logs/YYYY-MM/YYYY-MM-DD-<process>.jsonl. A
huggingface_hub CommitScheduler uploads the folder to LOG_REPO every LOG_EVERY_MIN minutes, and
a daily job deletes files older than RETENTION_DAYS from the repo.

Logged: UTC time, tool, scrubbed query, filters, result count, returned document ids, latency,
client application name/version, and a session id hashed with a daily rotating salt (counts
sessions per day without linking days). Never logged: IP addresses, raw queries, request headers.
Without LOG_REPO and HF_TOKEN, events go to stdout only.
"""
import datetime as dt, hashlib, json, os, secrets, threading, time, uuid

LOG_REPO = os.environ.get("LOG_REPO")
TOKEN = os.environ.get("HF_TOKEN")
LOCAL = os.environ.get("LOG_DIR", "/tmp/usage_logs")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "730"))
EVERY_MIN = float(os.environ.get("LOG_EVERY_MIN", "10"))


class UsageLog:
    def __init__(self):
        self.proc = uuid.uuid4().hex[:8]
        self.lock = threading.Lock()
        self.salt_day, self.salt = None, None
        self.scheduler = None
        if LOG_REPO and TOKEN:
            from huggingface_hub import CommitScheduler
            os.makedirs(f"{LOCAL}/logs", exist_ok=True)
            self.scheduler = CommitScheduler(repo_id=LOG_REPO, repo_type="dataset", folder_path=LOCAL,
                                             every=EVERY_MIN, private=True, token=TOKEN,
                                             allow_patterns=["logs/**/*.jsonl"])
            threading.Thread(target=self._retention_loop, daemon=True).start()

    def session_hash(self, session_id):
        if not session_id:
            return None
        today = dt.date.today()
        if self.salt_day != today:  # rotate daily; the salt is never stored
            self.salt_day, self.salt = today, secrets.token_bytes(16)
        return hashlib.sha256(self.salt + str(session_id).encode()).hexdigest()[:12]

    def write(self, event):
        now = dt.datetime.now(dt.timezone.utc)
        event = {"ts": now.isoformat(timespec="seconds"), **event}
        line = json.dumps(event, ensure_ascii=False)
        if not self.scheduler:
            print("usage", line, flush=True)
            return
        path = f"{LOCAL}/logs/{now:%Y-%m}/{now:%Y-%m-%d}-{self.proc}.jsonl"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self.scheduler.lock, open(path, "a") as f:
            f.write(line + "\n")

    def _retention_loop(self):
        from huggingface_hub import CommitOperationDelete, HfApi
        api = HfApi(token=TOKEN)
        while True:
            try:
                cutoff = (dt.date.today() - dt.timedelta(days=RETENTION_DAYS)).isoformat()
                old = [f for f in api.list_repo_files(LOG_REPO, repo_type="dataset")
                       if f.startswith("logs/") and f.endswith(".jsonl") and os.path.basename(f)[:10] < cutoff]
                if old:
                    api.create_commit(LOG_REPO, repo_type="dataset",
                                      operations=[CommitOperationDelete(f) for f in old],
                                      commit_message=f"Retention: delete {len(old)} log files older than {cutoff}")
            except Exception as e:  # retention must never take the service down
                print("retention error:", e, flush=True)
            time.sleep(86400)
