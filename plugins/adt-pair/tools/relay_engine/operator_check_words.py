"""Terminal confirmation rendering and measured sudo refusals."""

SUDO_REFUSALS = (
    ('is not in the sudoers file', 'linux', 'Debian sudo 1.9.16p2, IMPL 070854 row 8'),
    ('is not allowed to execute', 'none', 'documented sudo refusal, design section 4'),
)


def render_act_line(text: str) -> str:
    rendered = ''.join(f'\\x{ord(char):02x}' if ord(char) < 32 or 127 <= ord(char) <= 159 else char
                       for char in text)
    return rendered if len(rendered) <= 160 else rendered[:159] + '…'


def prompt_text(act_line: str) -> str:
    return 'Password to confirm: ' + act_line.replace('%', '%%') + ': '


def classify_sudo_failure(stderr: str) -> str:
    return 'not_permitted' if any(text in stderr for text, _, _ in SUDO_REFUSALS) else 'prove_failed'


def describe_act(purpose: str, args) -> str:
    if purpose in ('engine_start', 'engine_stop'):
        verb = 'Start' if purpose == 'engine_start' else 'Stop'
        return render_act_line(f'{verb} the engine for store {args.get("root", args.get("store_root", ""))}')
    return render_act_line(purpose + ' ' + ', '.join(f'{key}={value}' for key, value in sorted(args.items())))
