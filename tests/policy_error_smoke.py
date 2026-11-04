"""HTTP 404 propagates through each real role path, using simulated hardware."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from integration_smoke import Stack
from remote_teleop.config import atomic_json


class MissingPolicy(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        self.end_headers()

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(('127.0.0.1', 0), MissingPolicy)
threading.Thread(target=server.serve_forever, daemon=True).start()
results = []
try:
    for location in ('robot', 'client', 'cloud'):
        stack = Stack(location, '/home/siinuc2/miniconda3/envs/remote-controller/bin/python')
        assert stack.config.general.simulation
        stack.config.policy.backend = 'openpi'
        stack.config.policy.url = f'ws://127.0.0.1:{server.server_port}/must-not-leak/missing'
        for config_file in stack.configs.values():
            atomic_json(config_file, stack.config.model_dump())
        try:
            stack.start()
            job = stack.control('record_start', {'name': 'failed_endpoint'})
            stack.wait(lambda status: (status.get('record_job') or {}).get('id') == job['id']
                       and (status.get('record_job') or {}).get('state') == 'COMPLETE', 'recording ready', 90)
            stack.control('arm')
            stack.wait(lambda status: status.get('mode') == 'READY', 'armed')
            stack.control('start')
            status = stack.wait(lambda value: value.get('mode') == 'SAFE_HOLD', 'specific policy failure', 15)
            assert status['reason'] == 'POLICY_SERVICE_ERROR', status
            assert status['policy_error']['http_status'] == 404, status
            assert status['recorder']['frames'] == 0, status
            assert 'must-not-leak' not in json.dumps(status['policy_error'])
            results.append({'location': location, 'mode': status['mode'], 'reason': status['reason'],
                            'policy_error': status['policy_error'], 'frames': status['recorder']['frames']})
            print(json.dumps(results[-1]), flush=True)
        except Exception:
            print('FAILED; isolated logs: ' + str(stack.path), flush=True)
            raise
        finally:
            stack.close()
finally:
    server.shutdown()
report = {'passed': True, 'hardware_opened': False, 'results': results}
Path('state/policy-error-acceptance.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
