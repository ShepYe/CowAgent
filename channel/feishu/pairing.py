"""Desktop-initiated identity pairing; no message becomes business input here."""
import json
import secrets
import threading
import time

from common.personal_tasks import TaskError


class FeishuPairing:
    def __init__(self):
        self._lock = threading.RLock()
        self._state = {'state': 'idle'}
        self._revision_reader = None
        self._revision = None

    def _expire(self):
        if (self._state['state'] in {'waiting', 'pending'} and
                (time.monotonic() >= self._state['deadline'] or
                 (self._revision_reader and self._revision_reader() != self._revision))):
            self._state = {'state': 'expired'}

    def snapshot(self):
        with self._lock:
            self._expire()
            view = {key: value for key, value in self._state.items() if key != 'deadline'}
            if 'deadline' in self._state:
                view['expires_in'] = max(0, int(self._state['deadline'] - time.monotonic()))
            if 'candidate' in view:
                view['candidate'] = dict(view['candidate'])
            return view

    def begin(self, instance_id, app_id, revision_reader=None):
        with self._lock:
            self._revision_reader = revision_reader
            self._revision = revision_reader() if revision_reader else None
            self._state = {
                'state': 'waiting', 'pairing_id': secrets.token_urlsafe(24),
                'channel_instance_id': instance_id, 'app_id': app_id,
                'command': '/bind COW-' + secrets.token_hex(8).upper(),
                'deadline': time.monotonic() + 300,
            }
            return self.snapshot()

    def active_for(self, channel):
        with self._lock:
            self._expire()
            if (self._state['state'] in {'waiting', 'pending'} and
                    self._state['channel_instance_id'] == (channel.instance_id or 'feishu') and
                    self._state['app_id'] != channel.cfg('feishu_app_id')):
                self._state = {'state': 'expired'}
            return (self._state['state'] in {'waiting', 'pending'} and
                    self._state['channel_instance_id'] == (channel.instance_id or 'feishu') and
                    self._state['app_id'] == channel.cfg('feishu_app_id'))

    def capture(self, channel, event):
        """Consume only a matching private pairing command, without saving its content."""
        sender, message = event.get('sender') or {}, event.get('message') or {}
        binding_only = self.active_for(channel)
        content = message.get('content') or ''
        if message.get('message_type') != 'text' or not isinstance(content, str):
            return binding_only
        try:
            body = json.loads(content)
        except ValueError:
            return binding_only
        text = body.get('text') if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip().startswith('/bind COW-'):
            return binding_only
        # Pairing commands, including retries after confirmation/expiry, never
        # enter the agent or its retained conversation history.
        with self._lock:
            if not self.active_for(channel):
                return True
            if len(content) > 512:
                return True
            if (message.get('chat_type') != 'p2p' or message.get('message_type') != 'text' or
                    sender.get('sender_type') != 'user' or event.get('app_id') != self._state['app_id']):
                return True
            command = self._state.get('command')
            if not command or not secrets.compare_digest(text.strip().encode(), command.encode()):
                return True
            tenant = sender.get('tenant_key') or event.get('tenant_key')
            open_id = (sender.get('sender_id') or {}).get('open_id')
            if (not isinstance(tenant, str) or not tenant or len(tenant) > 256 or
                    not isinstance(open_id, str) or not open_id or len(open_id) > 256 or
                    (event.get('tenant_key') and event['tenant_key'] != tenant)):
                return True
            if self._state['state'] == 'waiting':
                self._state['candidate'] = {
                    'channel_instance_id': self._state['channel_instance_id'],
                    'app_id': self._state['app_id'], 'tenant_id': tenant, 'sender_open_id': open_id,
                }
                self._state['state'] = 'pending'
            # Re-delivery cannot replace the first candidate or become a task.
            return True

    def confirm(self, pairing_id, current_app_id, save):
        with self._lock:
            self._expire()
            if (self._state['state'] != 'pending' or self._state.get('pairing_id') != pairing_id or
                    self._state['app_id'] != current_app_id):
                raise TaskError('pairing_unavailable')
            binding = dict(self._state['candidate'])
            save(binding)  # Keep the candidate available if durable saving fails.
            self._state = {'state': 'confirmed'}
            return binding

    def check_revision(self):
        """Called again under the configuration save lock, before any write."""
        with self._lock:
            self._expire()
            if self._state['state'] != 'pending':
                raise TaskError('pairing_unavailable')

    def cancel(self, pairing_id):
        with self._lock:
            if self._state.get('pairing_id') == pairing_id:
                self._state = {'state': 'idle'}
            return self.snapshot()


pairing = FeishuPairing()


def configuration_revision():
    """Any saved config or roster change invalidates an unfinished handshake."""
    from pathlib import Path
    from config import conf, get_data_root
    from agent.team import team_file
    from common.settings_revision import current
    revision = [current()]
    for path in (Path(get_data_root()) / 'config.json', team_file(conf())):
        try:
            stat = path.stat()
            revision.append((stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except FileNotFoundError:
            revision.append(None)
    return tuple(revision)
