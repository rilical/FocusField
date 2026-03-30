from __future__ import annotations

import copy
import fnmatch
import multiprocessing as mp
import os
import queue
import threading
import time
from multiprocessing import shared_memory
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

from focusfield.audio.beamform.delay_and_sum import start_delay_and_sum
from focusfield.audio.beamform.mvdr import start_mvdr
from focusfield.audio.capture import start_audio_capture
from focusfield.audio.doa.srp_phat import start_srp_phat
from focusfield.audio.enhance.denoise import start_denoise
from focusfield.audio.mic_health import start_audio_mic_health
from focusfield.audio.vad import start_audio_vad
from focusfield.core.logging import LogEmitter
from focusfield.main.runtime_support import (
    apply_runtime_os_tuning,
    apply_runtime_thread_caps,
    build_bus,
    camera_topics,
    runtime_requirements,
    start_beamformed_passthrough,
)
from focusfield.vision.cameras import start_cameras
from focusfield.vision.speaker_heatmap import start_speaker_heatmap
from focusfield.vision.tracking.face_track import start_face_tracking


def start_multiprocess_runtime(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> List[threading.Thread]:
    runtime_cfg = config.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    mp_cfg = runtime_cfg.get("multiprocess", {})
    if not isinstance(mp_cfg, dict):
        mp_cfg = {}
    start_method = str(mp_cfg.get("start_method", "spawn") or "spawn").strip().lower()
    queue_depth = max(4, int(mp_cfg.get("queue_depth", 32) or 32))
    use_shared_memory = bool(mp_cfg.get("shared_memory", True))

    ctx = mp.get_context(start_method)
    stop_flag = ctx.Event()

    audio_out = ctx.Queue(maxsize=queue_depth)
    audio_in = ctx.Queue(maxsize=queue_depth)
    vision_out = ctx.Queue(maxsize=queue_depth)
    vision_in = ctx.Queue(maxsize=queue_depth)

    req = runtime_requirements(config)
    audio_proc = ctx.Process(
        target=_worker_main,
        name="focusfield-audio",
        args=("audio", copy.deepcopy(config), req, queue_depth, use_shared_memory, audio_out, audio_in, stop_flag),
        daemon=True,
    )
    vision_proc = ctx.Process(
        target=_worker_main,
        name="focusfield-vision",
        args=("vision", copy.deepcopy(config), req, queue_depth, use_shared_memory, vision_out, vision_in, stop_flag),
        daemon=True,
    )
    audio_proc.start()
    vision_proc.start()
    logger.emit(
        "info",
        "main.runtime_mp",
        "workers_started",
        {
            "start_method": start_method,
            "queue_depth": queue_depth,
            "shared_memory": use_shared_memory,
            "audio_pid": audio_proc.pid,
            "vision_pid": vision_proc.pid,
        },
    )

    threads = [
        _start_parent_reader(bus, logger, stop_event, stop_flag, audio_out, "audio", use_shared_memory),
        _start_parent_reader(bus, logger, stop_event, stop_flag, vision_out, "vision", use_shared_memory),
        _start_parent_forwarder(bus, config, stop_event, audio_in, "fusion.target_lock"),
        _start_parent_forwarder(bus, config, stop_event, vision_in, "vision.camera_calibration"),
        _start_process_supervisor(logger, stop_event, stop_flag, {"audio": audio_proc, "vision": vision_proc}),
    ]
    return threads


def _worker_main(
    role: str,
    config: Dict[str, Any],
    req: Dict[str, Any],
    queue_depth: int,
    use_shared_memory: bool,
    out_queue: mp.Queue,
    in_queue: Optional[mp.Queue],
    stop_flag: mp.synchronize.Event,
) -> None:
    stop_event = threading.Event()
    bus = build_bus(config)
    logger = LogEmitter(
        bus,
        min_level=config.get("logging", {}).get("level", "info"),
        run_id=str(config.get("runtime", {}).get("run_id", "")),
    )
    apply_runtime_thread_caps(config, logger, role=role)
    apply_runtime_os_tuning(config, logger, role=role)

    original_excepthook = threading.excepthook
    crash_flag = threading.Event()

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        logger.emit(
            "error",
            "main.runtime_mp",
            "worker_thread_crash",
            {
                "role": role,
                "thread": getattr(args.thread, "name", "<unknown>"),
                "type": getattr(args.exc_type, "__name__", str(args.exc_type)),
                "message": str(args.exc_value),
            },
        )
        crash_flag.set()
        stop_event.set()
        stop_flag.set()

    threading.excepthook = _thread_excepthook

    bridge_threads = _start_topic_forwarders(
        bus,
        logger,
        stop_event,
        out_queue,
        role,
        config,
        _worker_topics(role, config),
        queue_depth,
        use_shared_memory,
    )
    if in_queue is not None:
        bridge_threads.append(_start_inbound_relay(bus, stop_event, in_queue))
    bridge_threads.append(_start_stop_watcher(stop_event, stop_flag))

    module_threads: List[threading.Thread] = []
    if role == "audio":
        module_threads.extend(_start_audio_worker(bus, config, logger, stop_event))
    elif role == "vision":
        module_threads.extend(_start_vision_worker(bus, config, logger, stop_event, req))
    else:
        logger.emit("error", "main.runtime_mp", "worker_role_invalid", {"role": role})
        raise SystemExit(2)

    logger.emit(
        "info",
        "main.runtime_mp",
        "worker_ready",
        {"role": role, "pid": os.getpid(), "threads": len(module_threads)},
    )
    try:
        while not stop_event.is_set() and not crash_flag.is_set() and not stop_flag.is_set():
            time.sleep(0.1)
    finally:
        stop_event.set()
        threading.excepthook = original_excepthook

    if crash_flag.is_set():
        raise SystemExit(1)


def _start_audio_worker(bus: Any, config: Dict[str, Any], logger: Any, stop_event: threading.Event) -> List[threading.Thread]:
    threads: List[threading.Thread] = []
    audio_thread = start_audio_capture(bus, config, logger, stop_event)
    if audio_thread is not None:
        threads.append(audio_thread)
    mic_health_thread = start_audio_mic_health(bus, config, logger, stop_event)
    if mic_health_thread is not None:
        threads.append(mic_health_thread)
    vad_thread = start_audio_vad(bus, config, logger, stop_event)
    if vad_thread is not None:
        threads.append(vad_thread)
    doa_thread = start_srp_phat(bus, config, logger, stop_event)
    if doa_thread is not None:
        threads.append(doa_thread)
    beam_thread = start_mvdr(bus, config, logger, stop_event)
    if beam_thread is None:
        beam_thread = start_delay_and_sum(bus, config, logger, stop_event)
    if beam_thread is not None:
        threads.append(beam_thread)
    denoise_thread = start_denoise(bus, config, logger, stop_event)
    if denoise_thread is not None:
        threads.append(denoise_thread)
    else:
        threads.append(start_beamformed_passthrough(bus, logger, stop_event, "main.runtime_mp"))
    return threads


def _start_vision_worker(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
    req: Dict[str, Any],
) -> List[threading.Thread]:
    threads: List[threading.Thread] = []
    threads.extend(
        start_cameras(
            bus,
            config,
            logger,
            stop_event,
            strict_capture=bool(req.get("strict", False)),
            camera_scope=str(req.get("camera_scope", "any")),
        )
    )
    threads.append(start_face_tracking(bus, config, logger, stop_event))
    threads.append(start_speaker_heatmap(bus, config, logger, stop_event))
    return threads


def _worker_topics(role: str, config: Dict[str, Any]) -> List[str]:
    if role == "audio":
        return [
            "audio.frames",
            "audio.capture.stats",
            "audio.mic_health",
            "audio.vad",
            "audio.doa_heatmap",
            "audio.beamformer.debug",
            "audio.enhanced.final",
            "runtime.worker_loop",
            "log.events",
        ]
    if role == "vision":
        return camera_topics(config) + [
            "vision.face_tracks",
            "vision.speaker_heatmap",
            "runtime.worker_loop",
            "log.events",
        ]
    return ["log.events"]


def _start_parent_reader(
    bus: Any,
    logger: Any,
    stop_event: threading.Event,
    stop_flag: mp.synchronize.Event,
    out_queue: mp.Queue,
    role: str,
    use_shared_memory: bool,
) -> threading.Thread:
    def _run() -> None:
        while not stop_event.is_set() and not stop_flag.is_set():
            try:
                envelope = out_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.emit("warning", "main.runtime_mp", "worker_read_failed", {"role": role, "error": str(exc)})
                continue
            if not isinstance(envelope, dict):
                continue
            topic = str(envelope.get("topic", "") or "")
            if not topic:
                continue
            msg = envelope.get("msg")
            try:
                if use_shared_memory:
                    msg = _decode_payload(msg)
            except Exception as exc:  # noqa: BLE001
                logger.emit(
                    "warning",
                    "main.runtime_mp",
                    "worker_decode_failed",
                    {"role": role, "topic": topic, "error": str(exc)},
                )
                continue
            bus.publish(topic, msg)

    thread = threading.Thread(target=_run, name=f"runtime-mp-read-{role}", daemon=True)
    thread.start()
    return thread


def _start_parent_forwarder(
    bus: Any,
    config: Dict[str, Any],
    stop_event: threading.Event,
    in_queue: mp.Queue,
    topic: str,
) -> threading.Thread:
    q_topic = bus.subscribe(topic)
    policy = _topic_queue_policy(config, topic)

    def _run() -> None:
        while not stop_event.is_set():
            try:
                msg = q_topic.get(timeout=0.1)
            except queue.Empty:
                continue
            _put_queue_with_policy(in_queue, {"topic": topic, "msg": msg}, policy)

    thread = threading.Thread(target=_run, name=f"runtime-mp-fwd-{topic.replace('.', '-')}", daemon=True)
    thread.start()
    return thread


def _start_process_supervisor(
    logger: Any,
    stop_event: threading.Event,
    stop_flag: mp.synchronize.Event,
    processes: Dict[str, mp.Process],
) -> threading.Thread:
    def _run() -> None:
        try:
            while not stop_event.is_set():
                for role, proc in processes.items():
                    if proc.is_alive():
                        continue
                    exitcode = proc.exitcode
                    if stop_flag.is_set() or stop_event.is_set():
                        continue
                    logger.emit(
                        "error",
                        "main.runtime_mp",
                        "worker_exited",
                        {"role": role, "exitcode": exitcode},
                    )
                    stop_flag.set()
                    stop_event.set()
                    break
                time.sleep(0.1)
        finally:
            stop_flag.set()
            for proc in processes.values():
                proc.join(timeout=1.0)
                if proc.is_alive():
                    proc.terminate()
            for proc in processes.values():
                proc.join(timeout=1.0)

    thread = threading.Thread(target=_run, name="runtime-mp-supervisor", daemon=True)
    thread.start()
    return thread


def _start_topic_forwarders(
    bus: Any,
    logger: Any,
    stop_event: threading.Event,
    out_queue: mp.Queue,
    role: str,
    config: Dict[str, Any],
    topics: Iterable[str],
    queue_depth: int,
    use_shared_memory: bool,
) -> List[threading.Thread]:
    threads: List[threading.Thread] = []
    for topic in topics:
        q_topic = bus.subscribe(topic)
        policy = _topic_queue_policy(config, topic)

        def _run(q_local=q_topic, topic_name=topic) -> None:
            while not stop_event.is_set():
                try:
                    msg = q_local.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    payload = _encode_payload(msg) if use_shared_memory else msg
                    _put_queue_with_policy(out_queue, {"topic": topic_name, "msg": payload}, policy)
                except Exception as exc:  # noqa: BLE001
                    logger.emit(
                        "warning",
                        "main.runtime_mp",
                        "worker_forward_failed",
                        {"role": role, "topic": topic_name, "error": str(exc), "queue_depth": queue_depth},
                    )

        thread = threading.Thread(
            target=_run,
            name=f"runtime-mp-fwd-{role}-{topic.replace('.', '-')}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def _start_inbound_relay(bus: Any, stop_event: threading.Event, in_queue: mp.Queue) -> threading.Thread:
    def _run() -> None:
        while not stop_event.is_set():
            try:
                envelope = in_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if not isinstance(envelope, dict):
                continue
            topic = str(envelope.get("topic", "fusion.target_lock") or "fusion.target_lock")
            msg = envelope.get("msg", envelope)
            bus.publish(topic, msg)

    thread = threading.Thread(target=_run, name="runtime-mp-inbound", daemon=True)
    thread.start()
    return thread


def _start_stop_watcher(stop_event: threading.Event, stop_flag: mp.synchronize.Event) -> threading.Thread:
    def _run() -> None:
        while not stop_event.is_set() and not stop_flag.is_set():
            time.sleep(0.05)
        stop_event.set()

    thread = threading.Thread(target=_run, name="runtime-mp-stop", daemon=True)
    thread.start()
    return thread


def _topic_queue_policy(config: Dict[str, Any], topic: str) -> str:
    bus_cfg = config.get("bus", {})
    if not isinstance(bus_cfg, dict):
        bus_cfg = {}
    policies = bus_cfg.get("topic_queue_policies", {})
    if not isinstance(policies, dict):
        policies = {}
    configured = policies.get(topic)
    if configured is not None:
        return _normalize_policy(configured)
    wildcard_matches: list[tuple[str, Any]] = []
    for key, value in policies.items():
        if "*" in str(key) and fnmatch.fnmatch(topic, str(key)):
            wildcard_matches.append((str(key), value))
    if wildcard_matches:
        _best_key, best_value = max(wildcard_matches, key=lambda item: len(item[0]))
        return _normalize_policy(best_value)
    return "drop_oldest"


def _normalize_policy(value: Any) -> str:
    policy = str(value or "drop_oldest").strip().lower()
    if policy in {"drop_newest", "newest"}:
        return "drop_newest"
    return "drop_oldest"


def _put_queue_with_policy(q_obj: Any, msg: Any, policy: str) -> None:
    try:
        q_obj.put_nowait(msg)
        return
    except queue.Full:
        if policy == "drop_newest":
            _cleanup_encoded_payload(msg)
            return
        pass
    except Exception:
        try:
            q_obj.put(msg, timeout=0.05)
            return
        except Exception:
            _cleanup_encoded_payload(msg)
            return
    try:
        dropped = q_obj.get_nowait()
        _cleanup_encoded_payload(dropped)
    except Exception:
        pass
    try:
        q_obj.put_nowait(msg)
    except Exception:
        _cleanup_encoded_payload(msg)


def _put_queue_drop_oldest(q_obj: Any, msg: Any) -> None:
    _put_queue_with_policy(q_obj, msg, "drop_oldest")


def _encode_payload(msg: Any) -> Any:
    return _encode_value(msg)


def _encode_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        shm = shared_memory.SharedMemory(create=True, size=int(value.nbytes))
        view = np.ndarray(value.shape, dtype=value.dtype, buffer=shm.buf)
        view[...] = value
        shm.close()
        return {
            "__focusfield_shm__": True,
            "name": shm.name,
            "shape": list(value.shape),
            "dtype": str(value.dtype),
        }
    if isinstance(value, dict):
        return {str(k): _encode_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode_value(item) for item in value]
    if isinstance(value, tuple):
        return {"__focusfield_tuple__": [_encode_value(item) for item in value]}
    return value


def _decode_payload(payload: Any) -> Any:
    return _decode_value(payload)


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and value.get("__focusfield_shm__"):
        name = str(value["name"])
        shape = tuple(int(dim) for dim in value["shape"])
        dtype = np.dtype(str(value["dtype"]))
        shm = shared_memory.SharedMemory(name=name)
        try:
            arr = np.ndarray(shape, dtype=dtype, buffer=shm.buf).copy()
        finally:
            shm.close()
            try:
                shm.unlink()
            except FileNotFoundError:
                pass
        return arr
    if isinstance(value, dict) and "__focusfield_tuple__" in value:
        return tuple(_decode_value(item) for item in value["__focusfield_tuple__"])
    if isinstance(value, dict):
        return {key: _decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    return value


def _cleanup_encoded_payload(value: Any) -> None:
    if isinstance(value, dict) and value.get("__focusfield_shm__"):
        try:
            shm = shared_memory.SharedMemory(name=str(value["name"]))
            shm.close()
            shm.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return
    if isinstance(value, dict):
        for item in value.values():
            _cleanup_encoded_payload(item)
        return
    if isinstance(value, list):
        for item in value:
            _cleanup_encoded_payload(item)
