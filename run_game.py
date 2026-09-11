"""Run the game, the Python classifier and the AI layer together; Ctrl+C stops all."""
import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

from bridge.reader import latest_session

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / 'game_engine'
DEFAULT_MODEL = 'qwen/qwen3-vl-8b'


def lan_ip():
    """The address other laptops on the same Wi-Fi use to reach this one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))  # no packet is sent; this only picks a route
            return s.getsockname()[0]
    except OSError:
        return 'localhost'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--bots', type=int, default=0,
                        help='bot farm size. Keep 0 while collecting human data')
    parser.add_argument('--declare', choices=('human', 'bot'), default='human',
                        help='kept for compatibility; bots always connect as kind=bot '
                             'and are never labelled human')
    parser.add_argument('--required-categories', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--no-ai', dest='ai', action='store_false',
                        help='turn off both AI layers (Python adaptive + JS rule agent)')
    parser.add_argument('--ai', dest='ai', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--ai-dry-run', action='store_true',
                        help='AI reports what it would change but changes nothing')
    parser.add_argument('--model', default=os.environ.get('LOCAL_MODEL', DEFAULT_MODEL),
                        help=f'LM Studio model for both AI layers (default {DEFAULT_MODEL})')
    parser.add_argument('--collect-code', default=os.environ.get('COLLECT_CODE', 'insan'),
                        help='secret in the human data link; only players with it are labelled human')
    parser.add_argument('--collect-minutes', type=float, default=10)
    parser.set_defaults(ai=True)
    args = parser.parse_args()
    node = shutil.which('node')
    if not node or not (ENGINE / 'node_modules' / 'ws').is_dir():
        parser.error('Install Node and run npm ci in game_engine')
    processes = []
    logs = []
    trace_dir = ENGINE / 'traces'
    old = latest_session(trace_dir)
    env = dict(os.environ, PORT=str(args.port), TRACE_LOG='1',
               COLLECT_CODE=args.collect_code, COLLECT_MINUTES=str(args.collect_minutes),
               LOCAL_MODEL=args.model)
    if args.ai:
        # The JS rule agent defaults to the Claude API; there is no key here,
        # LM Studio is what runs on this machine.
        env.setdefault('AGENT_PROVIDER', 'local')
        env.setdefault('AGENT_MODEL', args.model)
    try:
        server = subprocess.Popen([node, 'server/index.js'], cwd=ENGINE, env=env)
        processes.append(server)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            trace = latest_session(trace_dir)
            if server.poll() is not None:
                raise RuntimeError('Game server failed to start (port busy? try --port 8081)')
            if trace and trace != old:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('Timed out waiting for game trace')
        log = (trace_dir / 'brain.log').open('w', encoding='utf-8')
        logs.append(log)
        command = [
            sys.executable, '-u', '-m', 'bridge', '--follow', '--trace', str(trace),
            '--output', str(trace_dir / 'verdicts.jsonl'),
            '--required-categories', str(args.required_categories)]
        if args.ai:
            command.append('--ai')
        if args.ai_dry_run:
            command.append('--ai-dry-run')
        processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
        if args.bots:
            processes.append(subprocess.Popen([node, 'bots/naive-bot.js', '--count', str(args.bots),
                                               '--host', f'localhost:{args.port}'], cwd=ENGINE))
        ip = lan_ip()
        print('\n' + '=' * 70, flush=True)
        print(f'  HUMAN DATA LINK (everyone opens this, plays {args.collect_minutes:g} min):', flush=True)
        print(f'    http://{ip}:{args.port}/?collect={args.collect_code}', flush=True)
        print(f'  Dashboard: http://{ip}:{args.port}/dashboard/?key=demo', flush=True)
        print(f'  AI: {"ON, " + args.model + " via LM Studio" if args.ai else "off"}'
              f'{" (dry run)" if args.ai_dry_run else ""}', flush=True)
        print(f'  Data: {trace_dir}  (human-sessions.jsonl, labels.json, session-*.jsonl)', flush=True)
        print('=' * 70 + '\n', flush=True)
        while all(p.poll() is None for p in processes):
            time.sleep(0.5)
        raise RuntimeError('A game process exited; inspect traces/brain.log')
    except KeyboardInterrupt:
        pass
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=10)
        for log in logs:
            log.close()


if __name__ == '__main__':
    main()
