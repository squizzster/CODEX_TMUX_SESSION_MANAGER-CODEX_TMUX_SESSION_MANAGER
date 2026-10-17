"""Keep Ctrl-C exit or detachment on the originating tmux client."""

from __future__ import annotations

import shlex

from .tmux_session_capability import (
    TmuxSessionCapability,
    combine_tmux_if_shell_conditions,
    runtime_destruction_if_shell_condition,
)

CTRL_C_OWNERSHIP_REJECTION = "Rodex refused Ctrl-C: runtime or pane ownership changed."
EXIT_COUNTDOWN_SECONDS = 4
_EXIT_SEQUENCE_OPTION = "@rodex_exit_sequence"


def shared_ctrl_c_binding_command(capability: TmuxSessionCapability) -> str:
    """Warn before private keyboard exit; detach shared clients immediately.

    Native tmux messages, a one-key table, and native timers retain the actual
    originating client. Every destructive action rechecks its current pane and
    runtime ownership, without an explicit client or primary-pane fallback.
    A fresh sequence makes timers from an aborted warning harmless on rearming.
    External process signals do not enter this keyboard binding.
    """
    admission = combine_tmux_if_shell_conditions(
        runtime_destruction_if_shell_condition(capability),
        "#{!=:#{client_name},}",
        "#{==:#{client_session},#{session_name}}",
    )
    rejection = shlex.join(("display-message", CTRL_C_OWNERSHIP_REJECTION))
    kill = shlex.join(("kill-session", "-t", capability.session_target))
    # The count omits a client already exiting even while its native command
    # queue remains connected. Require the sole roster entry to be this client
    # too, so its old timer cannot destroy a replacement's runtime.
    sole_origin = combine_tmux_if_shell_conditions(
        "#{==:#{session_attached},1}",
        "#{==:#{session_attached_list},#{client_name}}",
    )
    exit_or_detach = _native_if(sole_origin, kill, "detach-client")
    guarded_exit = _native_if(admission, exit_or_detach, rejection)
    table = f"rodex-exit-{capability.runtime_id}"
    # These bindings are part of the already claimed root C-c command, and are
    # installed only when that command admits a private client. tmux consumes
    # one key and returns to its normal table before running the selected action.
    arm = [
        shlex.join(("bind-key", "-T", table, "d", "detach-client")),
        f"bind-key -T {table} q {{ {guarded_exit} }}",
        shlex.join(("bind-key", "-T", table, "Any", "refresh-client -S")),
        shlex.join(("switch-client", "-T", table)),
        shlex.join(
            (
                "set-option",
                "-F",
                _EXIT_SEQUENCE_OPTION,
                f"#{{e|+|:#{{?{_EXIT_SEQUENCE_OPTION},#{{{_EXIT_SEQUENCE_OPTION}}},0}},1}}",
            )
        ),
        _countdown_message(EXIT_COUNTDOWN_SECONDS),
    ]
    pending = combine_tmux_if_shell_conditions(
        f"#{{==:#{{client_key_table}},{table}}}",
        f"#{{==:#{{{_EXIT_SEQUENCE_OPTION}}},RODEX_EXIT_SEQUENCE}}",
    )
    for elapsed in range(1, EXIT_COUNTDOWN_SECONDS + 1):
        remaining = EXIT_COUNTDOWN_SECONDS - elapsed
        action = _countdown_message(remaining) if remaining else f"switch-client -T root ; {guarded_exit}"
        callback = _native_if(pending, action)
        # run-shell -C is tmux's existing native timer, not a shell/helper job.
        # Freeze only this arm's sequence; all other formats stay live at expiry.
        deferred = callback.replace("#", "##").replace("RODEX_EXIT_SEQUENCE", f"#{{{_EXIT_SEQUENCE_OPTION}}}")
        arm.append(shlex.join(("run-shell", "-b", "-C", "-d", str(elapsed), deferred)))
    private_or_shared = _native_if("#{==:#{session_attached},1}", " ; ".join(arm), "detach-client")
    # Keep the top-level command arguments as strings for the existing exact
    # binding readback/ownership check; nested groups are parsed on keypress.
    return shlex.join(("if-shell", "-F", admission, private_or_shared, rejection))


def _native_if(condition: str, command: str, otherwise: str | None = None) -> str:
    """Use native command groups so nested bindings do not multiply quoting."""
    result = f"if-shell -F {shlex.quote(condition)} {{ {command} }}"
    if otherwise is not None:
        result += f" {{ {otherwise} }}"
    return result


def _countdown_message(seconds: int) -> str:
    message = (
        f"Rodex will 'EXIT' rather than 'DETACH' in {seconds} seconds: "
        "'d' to detach, 'q' to quit now, other key to abort."
    )
    return shlex.join(("display-message", "-d", "1100", message))
