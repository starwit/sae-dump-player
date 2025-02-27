import pathlib
import signal
import sys
import threading
import time

import pybase64
from visionapi.sae_pb2 import SaeMessage
from visionlib.pipeline.publisher import RedisPublisher
from visionlib.saedump import DumpMeta, Event, message_splitter


def register_stop_handler():
    stop_event = threading.Event()

    def sig_handler(signum, _):
        signame = signal.Signals(signum).name
        print(f'Caught signal {signame} ({signum}). Exiting...', file=sys.stderr)
        stop_event.set()

    signal.signal(signal.SIGTERM, sig_handler)
    signal.signal(signal.SIGINT, sig_handler)

    return stop_event

def wait_until(playback_start_time: float, record_start_time: float, record_target_time: float):
    current_time = time.time()
    playback_delta = current_time - playback_start_time
    target_delta = record_target_time - record_start_time
    if playback_delta <= target_delta:
        time.sleep(target_delta - playback_delta)

def set_frame_timestamp_to_now(proto_bytes: str):
    proto = SaeMessage()
    proto.ParseFromString(proto_bytes)

    proto.frame.timestamp_utc_ms = time.time_ns() // 1000000
    
    return proto.SerializeToString()

def play(file_path: pathlib.Path, redis_host: str, redis_port: int) -> None:

    stop_event = register_stop_handler()

    publish = RedisPublisher(redis_host, redis_port)

    with publish, open(file_path, 'r') as input_file:
        while True:
            messages = message_splitter(input_file)

            playback_start = time.time()
            start_message = next(messages)
            dump_meta = DumpMeta.model_validate_json(start_message)
            print(f'Starting playback from file {file_path} containing streams {dump_meta.recorded_streams}')

            for message in messages:
                event = Event.model_validate_json(message)
                proto_bytes = pybase64.standard_b64decode(event.data_b64)

                # Adjust timestamps to current time
                proto_bytes = set_frame_timestamp_to_now(proto_bytes)

                wait_until(playback_start, dump_meta.start_time, event.meta.record_time)

                publish(event.meta.source_stream, proto_bytes)

                if stop_event.is_set():
                    break
            
            if stop_event.is_set():
                break
            else:
                input_file.seek(0)
            