"""Read-only terminal viewer for redacted launch logs. Never opens the database."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from .observability import LOG_NAME, log_files


def symbol(value, fallback='-'):
    return value if type(value) is str and re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}', value) else fallback


def number(value):
    return str(round(value, 2)) if type(value) in (int, float) and 0 <= value <= 1000000000 else '-'


def format_event(event):
    """Treat log files as untrusted: no terminal escapes or arbitrary detail values."""
    if type(event) is not dict:
        return '[Skipped invalid event]'
    at = event.get('at', '')
    clock = at[11:23] if type(at) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT[0-9:.]+\+00:00', at) else '--:--:--'
    run = symbol(event.get('run_id'), 'application')[:12]
    stage, phase = symbol(event.get('stage')), symbol(event.get('phase'))
    detail = event.get('detail') if type(event.get('detail')) is dict else {}
    parts = [f'{clock} UTC  {run:12}  {stage}.{phase}', symbol(detail.get('name', event.get('operation')))]
    for key in ('intent', 'route', 'status', 'kind', 'decision', 'code'):
        if key in detail:
            parts.append(f'{key}={symbol(detail[key])}')
    for key in ('duration_ms', 'tool_calls', 'attempt', 'attempts', 'http_status'):
        if key in detail:
            parts.append(f'{key}={number(detail[key])}')
    usage = detail.get('usage')
    if type(usage) is dict and 'total_tokens' in usage:
        parts.append('tokens=' + number(usage['total_tokens']))
    if detail.get('logging_error'):
        parts.append('LOG WRITE FAILED')
    if event.get('parent_span'):
        parts.append('parent=' + symbol(event['parent_span']))
    return ' | '.join(parts)


class LogReader:
    def __init__(self, directory, boot_id):
        self.directory, self.boot_id = Path(directory), boot_id
        self.positions, self.partial = {}, {}
        self.ended = False
        self.last_segment = 0

    def poll(self):
        output = []
        try:
            paths = log_files(self.directory, self.boot_id)
        except OSError:
            return ['[Log directory unavailable; retrying]']
        for path in paths:
            segment = int(LOG_NAME.fullmatch(path.name)[2])
            if path not in self.positions and segment > self.last_segment + 1:
                output.append('[Earlier log segments are no longer retained]')
            self.last_segment = max(self.last_segment, segment)
            try:
                with path.open('rb') as handle:
                    position = self.positions.get(path, 0)
                    if path.stat().st_size < position:
                        position = 0
                        self.partial.pop(path, None)
                        output.append('[Log was truncated; reading retained content]')
                    handle.seek(position)
                    data = self.partial.pop(path, b'') + handle.read(256*1024)
                    self.positions[path] = handle.tell()
                lines = data.split(b'\n')
                remainder = lines.pop()
                if len(remainder) <= 65536:
                    self.partial[path] = remainder
                else:
                    output.append('[Skipped oversized log line]')
                for line in lines:
                    try:
                        if len(line) > 65536:
                            raise ValueError()
                        event = json.loads(line)
                        if type(event) is not dict or event.get('boot_id') != self.boot_id:
                            raise ValueError()
                        output.append(format_event(event))
                        if event.get('stage') == 'application' and event.get('phase') == 'end':
                            self.ended = True
                    except (ValueError, UnicodeError, RecursionError):
                        output.append('[Skipped invalid log line]')
                if self.positions[path] < path.stat().st_size:
                    break  # Drain older segments before displaying later events.
            except OSError:
                output.append('[Log segment became unavailable]')
        return output

    def caught_up(self):
        try:
            return all(self.positions.get(p, 0) >= p.stat().st_size for p in log_files(self.directory, self.boot_id))
        except OSError:
            return True


def parent_alive(pid):
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, no mutation rights.
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of exit.
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def launch_monitor(directory, boot_id):
    if sys.platform != 'win32':
        return False
    try:
        subprocess.Popen([sys.executable, '-B', '-m', 'dcta.monitor', '--directory', str(directory),
                          '--boot', boot_id, '--follow', '--parent-pid', str(os.getpid())],
                         cwd=Path(__file__).resolve().parents[1], creationflags=subprocess.CREATE_NEW_CONSOLE)
        return True
    except OSError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description='DCTA read-only tool-chain monitor (redacted operational logs).')
    parser.add_argument('--directory', default='work/runtime-logs/deepseek')
    parser.add_argument('--boot', help='Launch ID from --list; leave empty for the latest retained launch.')
    parser.add_argument('--list', action='store_true', help='List retained launches without opening the database.')
    parser.add_argument('--follow', action='store_true', help='Follow live events; Ctrl+C closes only this viewer.')
    parser.add_argument('--parent-pid', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.boot and not re.fullmatch(r'[0-9a-f]{32}', args.boot):
        parser.error('Use a 32-character launch ID from --list.')
    if args.parent_pid is not None and args.parent_pid <= 0:
        parser.error('Invalid parent process.')
    directory = Path(args.directory).absolute()
    try:
        paths = log_files(directory)
    except OSError:
        paths = []
    boots = {}
    for path in paths:
        match = LOG_NAME.fullmatch(path.name)
        boots.setdefault(match[1], []).append(path)
    if args.list:
        print('Retained application launches (UTC). Missing shutdown markers do not imply success.')
        for boot, files in reversed(list(boots.items())):
            print(f'{files[0].name[8:24]}  {boot}  {len(files)} segment(s)')
        if not boots:
            print('No retained logs. Start Run-GUI.ps1 first.')
        return 0
    boot = args.boot or (next(reversed(boots)) if boots else None)
    if not boot or boot not in boots:
        print('No retained log for this launch. Use --list to select one.')
        return 1
    print('DCTA RUNTIME MONITOR | Read-only | Ctrl+C closes this viewer only', flush=True)
    print('Launch: '+boot+'\nLogs: '+str(directory), flush=True)
    print('Raw text, amounts, recipient details, credentials and signatures are omitted.', flush=True)
    reader = LogReader(directory, boot)
    try:
        while True:
            for line in reader.poll():
                print(line, flush=True)
            if reader.caught_up():
                if reader.ended:
                    print('Application shutdown recorded.', flush=True)
                    break
                if not args.follow:
                    print('No shutdown marker in retained logs; launch may be active or interrupted.', flush=True)
                    break
                if args.parent_pid and not parent_alive(args.parent_pid):
                    print('Application process ended without a shutdown marker; unfinished requests have an unknown outcome.', flush=True)
                    break
                time.sleep(0.5)
    except KeyboardInterrupt:
        print('\nMonitor closed. The application is unaffected.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
