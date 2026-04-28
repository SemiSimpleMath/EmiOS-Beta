import sys
import threading
import traceback


def print_thread_info():
    for thread in threading.enumerate():
        print(f"Thread Name: {thread.name}, Ident: {thread.ident}, Daemon: {thread.daemon}")
        # Optional: Get the current stack trace for the thread
        frame = sys._current_frames().get(thread.ident)
        if frame:
            stack_trace = "".join(traceback.format_stack(frame))
            print(f"Stack trace for {thread.name}:\n{stack_trace}")