"""
PC Rent - RunPod Fleet Provisioner (v1)

Maintains a desired count of Linux GPU workers on RunPod by:
1) creating pods,
2) waiting for SSH readiness,
3) starting local SSHMachineWorker threads against those pods,
4) terminating pods that stay idle beyond a timeout.

Usage:
    python provisioner.py --count 10 --backend https://your-server.com
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import paramiko

try:
    import runpod
except ImportError as exc:
    raise SystemExit(
        "Missing dependency 'runpod'. Install with: pip install -r agent/requirements.txt"
    ) from exc


def _load_env_file(path: Path):
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if (
                len(value) >= 2 and (
                    (value[0] == '"' and value[-1] == '"') or
                    (value[0] == "'" and value[-1] == "'")
                )
            ):
                value = value[1:-1]
            # Keep explicit shell env higher priority than .env defaults.
            os.environ.setdefault(key, value)
    except Exception as exc:
        raise RuntimeError(f"Failed to read env file '{path}': {exc}") from exc


_load_env_file(Path(__file__).with_name(".env"))

import ssh_agent
from ssh_agent import SSHMachineWorker


DEFAULT_BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")
DEFAULT_GPU_TYPE = "NVIDIA RTX A5000"
DEFAULT_CLOUD_TYPE = "COMMUNITY"
DEFAULT_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
DEFAULT_DISK_GB = 20
DEFAULT_IDLE_TIMEOUT_MINUTES = 20
DEFAULT_RECONCILE_SECONDS = 15
DEFAULT_RUNTIME_READY_TIMEOUT_SECONDS = 8 * 60
DEFAULT_SSH_READY_TIMEOUT_SECONDS = 3 * 60
DEFAULT_STATE_FILE = Path(__file__).with_name("provisioner_state.json")


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(event: str, **fields: Any):
    payload = {"ts": _iso_utc_now(), "event": event, **fields}
    print("[PROVISIONER] " + json.dumps(payload, sort_keys=True), flush=True)


class RunPodAdapter:
    """Provider adapter (RunPod-only in v1)."""

    def __init__(self, api_key: str):
        runpod.api_key = api_key

    @staticmethod
    def _extract_pod_id(created: Any) -> str:
        if isinstance(created, str):
            return created
        if isinstance(created, dict):
            pod_id = created.get("id") or created.get("podId") or created.get("pod_id")
            if isinstance(pod_id, str) and pod_id:
                return pod_id
        raise RuntimeError(f"RunPod create_pod returned unexpected payload: {created!r}")

    def create_pod(
        self,
        *,
        name: str,
        image: str,
        gpu_type: str,
        cloud_type: str,
        disk_gb: int,
        env: dict[str, str] | None = None,
    ) -> str:
        created = runpod.create_pod(
            name=name,
            image_name=image,
            gpu_type_id=gpu_type,
            gpu_count=1,
            cloud_type=cloud_type,
            support_public_ip=True,
            start_ssh=True,
            container_disk_in_gb=disk_gb,
            ports="22/tcp",
            env=env or {},
        )
        return self._extract_pod_id(created)

    def get_pod(self, pod_id: str) -> dict[str, Any] | None:
        try:
            pod = runpod.get_pod(pod_id)
            if isinstance(pod, dict):
                return pod
            return None
        except Exception as exc:
            _log("runpod.get_pod.error", pod_id=pod_id, error=str(exc))
            return None

    def terminate_pod(self, pod_id: str) -> bool:
        try:
            runpod.terminate_pod(pod_id)
            return True
        except Exception as exc:
            _log("runpod.terminate_pod.error", pod_id=pod_id, error=str(exc))
            return False

    @staticmethod
    def _port_to_int(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        try:
            s = str(value).strip()
        except Exception:
            return None
        if not s:
            return None
        # Accept values like "22/tcp", "22", "tcp/22".
        digits = "".join(ch if ch.isdigit() else " " for ch in s).split()
        for token in digits:
            try:
                return int(token)
            except ValueError:
                continue
        return None

    @staticmethod
    def extract_ssh_endpoint(pod: dict[str, Any]) -> tuple[str, int] | None:
        runtime = pod.get("runtime") if isinstance(pod.get("runtime"), dict) else {}

        ports = runtime.get("ports")
        if isinstance(ports, list):
            for entry in ports:
                if not isinstance(entry, dict):
                    continue
                private_port = entry.get("privatePort")
                if private_port is None:
                    private_port = entry.get("private_port")
                if private_port is None:
                    private_port = entry.get("containerPort")
                private_port_int = RunPodAdapter._port_to_int(private_port)
                if private_port_int != 22:
                    continue
                host = (
                    entry.get("ip")
                    or entry.get("publicIp")
                    or runtime.get("publicIp")
                    or runtime.get("public_ip")
                    or pod.get("publicIp")
                    or pod.get("public_ip")
                )
                public_port = (
                    entry.get("publicPort")
                    or entry.get("public_port")
                    or entry.get("externalPort")
                    or entry.get("hostPort")
                )
                public_port_int = RunPodAdapter._port_to_int(public_port)
                try:
                    if host and public_port_int:
                        return str(host), int(public_port_int)
                except (TypeError, ValueError):
                    continue

        # Some responses may map ports by key, e.g. {"22/tcp":[{...}]}
        if isinstance(ports, dict):
            for k, entries in ports.items():
                private_port_int = RunPodAdapter._port_to_int(k)
                if private_port_int != 22:
                    continue
                if isinstance(entries, list):
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        host = (
                            entry.get("ip")
                            or entry.get("publicIp")
                            or runtime.get("publicIp")
                            or runtime.get("public_ip")
                            or pod.get("publicIp")
                            or pod.get("public_ip")
                        )
                        public_port = (
                            entry.get("publicPort")
                            or entry.get("public_port")
                            or entry.get("externalPort")
                            or entry.get("hostPort")
                        )
                        public_port_int = RunPodAdapter._port_to_int(public_port)
                        if host and public_port_int:
                            return str(host), int(public_port_int)
                elif isinstance(entries, dict):
                    host = (
                        entries.get("ip")
                        or entries.get("publicIp")
                        or runtime.get("publicIp")
                        or runtime.get("public_ip")
                        or pod.get("publicIp")
                        or pod.get("public_ip")
                    )
                    public_port = (
                        entries.get("publicPort")
                        or entries.get("public_port")
                        or entries.get("externalPort")
                        or entries.get("hostPort")
                    )
                    public_port_int = RunPodAdapter._port_to_int(public_port)
                    if host and public_port_int:
                        return str(host), int(public_port_int)

        port_mappings = runtime.get("portMappings")
        if isinstance(port_mappings, dict):
            public_port = port_mappings.get("22") or port_mappings.get(22)
            host = (
                runtime.get("publicIp")
                or runtime.get("public_ip")
                or pod.get("publicIp")
                or pod.get("public_ip")
            )
            public_port_int = RunPodAdapter._port_to_int(public_port)
            try:
                if host and public_port_int:
                    return str(host), int(public_port_int)
            except (TypeError, ValueError):
                pass

        return None


class Provisioner:
    def __init__(self, args: argparse.Namespace):
        self.desired_count = args.count
        self.keep_warm = bool(args.keep_warm)
        self.backend = args.backend
        self.gpu_type = args.gpu_type
        self.cloud_type = args.cloud_type
        self.image = args.image
        self.disk_gb = args.disk_gb
        self.ssh_key = os.path.expanduser(args.ssh_key)
        self.idle_timeout_seconds = int(args.idle_timeout_minutes * 60)
        self.reconcile_seconds = max(5, int(args.reconcile_seconds))
        self.runtime_timeout_seconds = DEFAULT_RUNTIME_READY_TIMEOUT_SECONDS
        self.ssh_timeout_seconds = DEFAULT_SSH_READY_TIMEOUT_SECONDS
        self.state_file = Path(args.state_file).expanduser()

        api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("RUNPOD_API_KEY is required")
        if not os.path.isfile(self.ssh_key):
            raise RuntimeError(f"SSH key not found: {self.ssh_key}")

        self.adapter = RunPodAdapter(api_key)
        self.stop_event = threading.Event()
        self._cleanup_done = False
        self._create_failures = 0
        self._next_create_allowed_at = 0.0
        self._allow_replenish = True
        self.records: dict[str, dict[str, Any]] = {}

        # Reuse existing worker stack against RunPod pods.
        ssh_agent.BACKEND_URL = self.backend

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            _log("state.load.error", path=str(self.state_file), error=str(exc))
            return

        rows = data.get("managed") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return

        now = time.time()
        loaded = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            pod_id = row.get("pod_id")
            if not isinstance(pod_id, str) or not pod_id:
                continue
            created_at = float(row.get("created_at") or now)
            rec = {
                "pod_id": pod_id,
                "label": row.get("label") or f"RunPod {self.gpu_type}",
                "host": row.get("host"),
                "port": row.get("port"),
                "worker_state": row.get("worker_state") or "connecting",
                "created_at": created_at,
                "last_seen_at": float(row.get("last_seen_at") or now),
                "last_available_at": row.get("last_available_at"),
                "last_job_started_at": row.get("last_job_started_at"),
                "last_job_finished_at": row.get("last_job_finished_at"),
                "machine_id": row.get("machine_id"),
                "runtime_deadline": now + self.runtime_timeout_seconds,
                "ssh_deadline": now + self.ssh_timeout_seconds,
                "worker": None,
                "thread": None,
            }
            self.records[pod_id] = rec
            loaded += 1

        if loaded:
            _log("state.loaded", path=str(self.state_file), records=loaded)

    def _record_to_json(self, rec: dict[str, Any]) -> dict[str, Any]:
        return {
            "pod_id": rec["pod_id"],
            "label": rec.get("label"),
            "host": rec.get("host"),
            "port": rec.get("port"),
            "worker_state": rec.get("worker_state"),
            "created_at": rec.get("created_at"),
            "last_seen_at": rec.get("last_seen_at"),
            "last_available_at": rec.get("last_available_at"),
            "last_job_started_at": rec.get("last_job_started_at"),
            "last_job_finished_at": rec.get("last_job_finished_at"),
            "machine_id": rec.get("machine_id"),
        }

    def _save_state(self):
        payload = {
            "version": 1,
            "managed": [self._record_to_json(rec) for rec in sorted(self.records.values(), key=lambda r: r["pod_id"])],
        }
        tmp_path = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.state_file)

    def _new_record(self, pod_id: str, label: str) -> dict[str, Any]:
        now = time.time()
        return {
            "pod_id": pod_id,
            "label": label,
            "host": None,
            "port": None,
            "worker_state": "connecting",
            "created_at": now,
            "last_seen_at": now,
            "last_available_at": None,
            "last_job_started_at": None,
            "last_job_finished_at": None,
            "machine_id": None,
            "runtime_deadline": now + self.runtime_timeout_seconds,
            "ssh_deadline": now + self.ssh_timeout_seconds,
            "last_wait_log_at": 0.0,
            "worker": None,
            "thread": None,
        }

    def _ssh_connectable(self, host: str, port: int) -> bool:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=port,
                username="root",
                key_filename=self.ssh_key,
                timeout=8,
            )
            return True
        except Exception:
            return False
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _start_worker(self, rec: dict[str, Any]):
        cfg = {
            "host": rec["host"],
            "port": rec["port"],
            "username": "root",
            "key_path": self.ssh_key,
            "password": None,
            "label": rec["label"],
            "machine_key_seed": f"runpod:{rec['pod_id']}",
        }
        worker = SSHMachineWorker(cfg)
        thread = threading.Thread(
            target=worker.run,
            daemon=True,
            name=f"runpod-worker-{rec['pod_id'][:8]}",
        )
        rec["worker"] = worker
        rec["thread"] = thread
        rec["worker_state"] = "connecting"
        thread.start()
        _log(
            "worker.started",
            pod_id=rec["pod_id"],
            label=rec["label"],
            host=rec["host"],
            port=rec["port"],
        )

    def _stop_worker(self, rec: dict[str, Any], join_timeout: int = 30):
        worker = rec.get("worker")
        thread = rec.get("thread")
        if worker:
            try:
                worker.stop()
            except Exception:
                pass
        if thread and thread.is_alive():
            thread.join(timeout=join_timeout)
        rec["worker"] = None
        rec["thread"] = None

    def _terminate_record(self, pod_id: str, reason: str):
        rec = self.records.get(pod_id)
        if not rec:
            return
        _log("pod.terminate.start", pod_id=pod_id, reason=reason, label=rec.get("label"))
        self._stop_worker(rec, join_timeout=30)
        self.adapter.terminate_pod(pod_id)
        self.records.pop(pod_id, None)
        if reason == "idle_timeout" and not self.keep_warm:
            has_non_idle = any(
                (rec.get("worker_state") in {"running", "setup", "connecting"})
                for rec in self.records.values()
            )
            if not has_non_idle:
                self._allow_replenish = False
                _log("scale.replenish.disabled", reason="idle_timeout_drain")
        _log("pod.terminate.done", pod_id=pod_id, reason=reason)

    def _remove_record_without_terminate(self, pod_id: str, reason: str):
        rec = self.records.get(pod_id)
        if not rec:
            return
        self._stop_worker(rec, join_timeout=10)
        self.records.pop(pod_id, None)
        _log("record.removed", pod_id=pod_id, reason=reason)

    def _refresh_worker_snapshot(self, rec: dict[str, Any]):
        worker = rec.get("worker")
        if not worker:
            return
        snapshot = worker.get_observability_snapshot()
        rec["worker_state"] = snapshot.get("state") or rec.get("worker_state")
        rec["last_available_at"] = snapshot.get("last_available_at")
        rec["last_job_started_at"] = snapshot.get("last_job_started_at")
        rec["last_job_finished_at"] = snapshot.get("last_job_finished_at")
        rec["machine_id"] = snapshot.get("machine_id")
        rec["last_seen_at"] = time.time()

    def _handle_existing_record(self, rec: dict[str, Any]):
        now = time.time()
        pod_id = rec["pod_id"]
        pod = self.adapter.get_pod(pod_id)
        if not pod:
            self._remove_record_without_terminate(pod_id, "pod_not_found")
            return

        rec["last_seen_at"] = now
        endpoint = self.adapter.extract_ssh_endpoint(pod)
        if endpoint:
            rec["host"], rec["port"] = endpoint

        if rec.get("worker") is None:
            if not endpoint:
                if now - float(rec.get("last_wait_log_at") or 0.0) >= 30.0:
                    rec["last_wait_log_at"] = now
                    _log("pod.waiting_runtime", pod_id=pod_id)
                if now > rec["runtime_deadline"]:
                    self._terminate_record(pod_id, "runtime_ready_timeout")
                return

            if (rec.get("host"), rec.get("port")) != endpoint:
                rec["host"], rec["port"] = endpoint
                _log("pod.ssh_endpoint", pod_id=pod_id, host=rec["host"], port=rec["port"])

            if now > rec["ssh_deadline"]:
                self._terminate_record(pod_id, "ssh_ready_timeout")
                return

            if not self._ssh_connectable(rec["host"], int(rec["port"])):
                if now - float(rec.get("last_wait_log_at") or 0.0) >= 30.0:
                    rec["last_wait_log_at"] = now
                    _log("pod.waiting_ssh", pod_id=pod_id, host=rec["host"], port=rec["port"])
                return

            self._start_worker(rec)
            return

        self._refresh_worker_snapshot(rec)
        worker_state = rec.get("worker_state")
        thread = rec.get("thread")
        if thread and not thread.is_alive():
            if rec.get("last_available_at") is None:
                self._terminate_record(pod_id, "worker_failed_before_available")
            else:
                self._terminate_record(pod_id, "worker_thread_exited")
            return

        # Allow brief recovery window for setup failures before replacement.
        worker = rec.get("worker")
        snapshot = worker.get_observability_snapshot() if worker else {}
        if worker_state == "error" and rec.get("last_available_at") is None:
            state_changed_at = snapshot.get("state_changed_at") or now
            if now - float(state_changed_at) >= 30:
                self._terminate_record(pod_id, "worker_error_before_available")
                return

        if worker_state == "available":
            last_available = rec.get("last_available_at")
            if last_available and now - float(last_available) >= self.idle_timeout_seconds:
                self._terminate_record(pod_id, "idle_timeout")

    def _count_managed(self) -> int:
        # Count all currently managed pods (including pods still booting),
        # to avoid over-provisioning while readiness checks are pending.
        return len(self.records)

    def _current_backoff_seconds(self) -> int:
        if self._create_failures <= 1:
            return 5
        if self._create_failures == 2:
            return 15
        if self._create_failures == 3:
            return 30
        return 60

    def _maybe_scale_up(self):
        if not self.keep_warm and not self._allow_replenish:
            return

        now = time.time()
        managed = self._count_managed()
        missing = self.desired_count - managed
        if missing <= 0:
            return

        if now < self._next_create_allowed_at:
            return

        for _ in range(missing):
            pod_name = f"pcrent-worker-{uuid4().hex[:10]}"
            label = f"RunPod {self.gpu_type} #{pod_name[-4:]}"
            try:
                pod_id = self.adapter.create_pod(
                    name=pod_name,
                    image=self.image,
                    gpu_type=self.gpu_type,
                    cloud_type=self.cloud_type,
                    disk_gb=self.disk_gb,
                    env={"PCRENT_MANAGED": "1"},
                )
            except Exception as exc:
                self._create_failures += 1
                backoff = self._current_backoff_seconds()
                self._next_create_allowed_at = time.time() + backoff
                _log(
                    "pod.create.error",
                    error=str(exc),
                    failures=self._create_failures,
                    backoff_seconds=backoff,
                )
                break

            self._create_failures = 0
            self._next_create_allowed_at = 0.0
            rec = self._new_record(pod_id, label)
            self.records[pod_id] = rec
            self._allow_replenish = True
            _log("pod.created", pod_id=pod_id, name=pod_name, label=label)

    def _maybe_scale_down(self):
        managed = self._count_managed()
        excess = managed - self.desired_count
        if excess <= 0:
            return

        candidates: list[dict[str, Any]] = []
        for rec in self.records.values():
            worker_state = rec.get("worker_state")
            if worker_state == "running":
                continue
            candidates.append(rec)

        # Trim newest non-running workers first.
        candidates.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
        for rec in candidates[:excess]:
            self._terminate_record(rec["pod_id"], "scale_down")

    def reconcile_once(self):
        pod_ids = list(self.records.keys())
        for pod_id in pod_ids:
            rec = self.records.get(pod_id)
            if not rec:
                continue
            try:
                self._handle_existing_record(rec)
            except Exception as exc:
                _log("reconcile.record.error", pod_id=pod_id, error=str(exc))

        self._maybe_scale_down()
        self._maybe_scale_up()
        self._save_state()
        _log("reconcile.summary", desired=self.desired_count, managed=len(self.records))

    def request_stop(self, reason: str):
        _log("stop.requested", reason=reason)
        self.stop_event.set()

    def cleanup(self):
        if self._cleanup_done:
            return
        self._cleanup_done = True
        _log("cleanup.start", managed=len(self.records))
        self.stop_event.set()

        pod_ids = list(self.records.keys())
        for pod_id in pod_ids:
            rec = self.records.get(pod_id)
            if not rec:
                continue
            self._stop_worker(rec, join_timeout=30)

        for pod_id in list(self.records.keys()):
            self.adapter.terminate_pod(pod_id)
            self.records.pop(pod_id, None)

        self._save_state()
        _log("cleanup.done")

    def run(self):
        self._load_state()
        _log(
            "provisioner.start",
            backend=self.backend,
            desired_count=self.desired_count,
            keep_warm=self.keep_warm,
            gpu_type=self.gpu_type,
            cloud_type=self.cloud_type,
            image=self.image,
            disk_gb=self.disk_gb,
            idle_timeout_minutes=self.idle_timeout_seconds // 60,
            reconcile_seconds=self.reconcile_seconds,
            state_file=str(self.state_file),
        )

        while not self.stop_event.is_set():
            try:
                self.reconcile_once()
            except Exception as exc:
                _log("reconcile.loop.error", error=str(exc))
            self.stop_event.wait(self.reconcile_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PC Rent RunPod Fleet Provisioner")
    parser.add_argument("--count", type=int, required=True, help="Desired number of managed workers")
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help=f"Backend URL (default: {DEFAULT_BACKEND})")
    parser.add_argument("--gpu-type", default=DEFAULT_GPU_TYPE, help=f"RunPod GPU type id (default: {DEFAULT_GPU_TYPE})")
    parser.add_argument(
        "--cloud-type",
        default=DEFAULT_CLOUD_TYPE,
        choices=["ALL", "COMMUNITY", "SECURE"],
        help=f"RunPod cloud type (default: {DEFAULT_CLOUD_TYPE})",
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"Pod image (default: {DEFAULT_IMAGE})")
    parser.add_argument("--disk-gb", type=int, default=DEFAULT_DISK_GB, help=f"Container disk in GB (default: {DEFAULT_DISK_GB})")
    parser.add_argument("--ssh-key", default="~/.ssh/id_ed25519", help="Path to SSH private key")
    parser.add_argument(
        "--idle-timeout-minutes",
        type=int,
        default=DEFAULT_IDLE_TIMEOUT_MINUTES,
        help=f"Idle timeout before terminate (default: {DEFAULT_IDLE_TIMEOUT_MINUTES})",
    )
    parser.add_argument(
        "--reconcile-seconds",
        type=int,
        default=DEFAULT_RECONCILE_SECONDS,
        help=f"Reconcile interval in seconds (default: {DEFAULT_RECONCILE_SECONDS})",
    )
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_FILE),
        help=f"Path to provisioner state JSON (default: {DEFAULT_STATE_FILE})",
    )
    parser.add_argument(
        "--keep-warm",
        action="store_true",
        help="Keep maintaining --count even when idle (default is drain-to-zero to avoid idle RunPod cost).",
    )
    args = parser.parse_args()

    if args.count < 0:
        parser.error("--count must be >= 0")
    if args.disk_gb < 1:
        parser.error("--disk-gb must be >= 1")
    if args.idle_timeout_minutes < 1:
        parser.error("--idle-timeout-minutes must be >= 1")
    if args.reconcile_seconds < 5:
        parser.error("--reconcile-seconds must be >= 5")
    return args


def main():
    args = parse_args()
    provisioner = Provisioner(args)

    def _signal_handler(signum, _frame):
        provisioner.request_stop(f"signal_{signum}")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    atexit.register(provisioner.cleanup)

    try:
        provisioner.run()
    finally:
        provisioner.cleanup()


if __name__ == "__main__":
    main()
