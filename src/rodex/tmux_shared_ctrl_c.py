"""Keep Ctrl-C exit or detachment on the originating tmux client."""

from __future__ import annotations

import shlex

from .tmux_session_capability import (
    TmuxSessionCapability,
    combine_tmux_if_shell_conditions,
    runtime_destruction_if_shell_condition,
)

CTRL_C_OWNERSHIP_REJECTION = "Rodex refused Ctrl-C: runtime or pane ownership changed."


def shared_ctrl_c_binding_command(capability: TmuxSessionCapability) -> str:
    """Build an immediate native binding: private exit, shared client detachment.

    tmux checks the actual client object before dispatching its queued key event.
    The binding's native commands then drain that queue without yielding to a
    shell job. A private client's kill therefore uses the same admission as its
    attachment count. A shared client's Ctrl-C only detaches that client. There
    is no deferred destructive callback, session-wide arming state, helper, or
    expiry timer that could retain authority after the client leaves.

    Admission validates the entire destructive capability in the originating
    client's current pane. The binding deliberately has no explicit pane target:
    choosing the primary independently of the client's pane would grant an
    observer or unrelated pane destructive authority.
    """
    admission = combine_tmux_if_shell_conditions(
        runtime_destruction_if_shell_condition(capability),
        "#{!=:#{client_name},}",
        "#{==:#{client_session},#{session_name}}",
    )
    rejection = shlex.join(("display-message", CTRL_C_OWNERSHIP_REJECTION))
    kill = shlex.join(("kill-session", "-t", capability.session_target))
    private_or_shared = shlex.join(("if-shell", "-F", "#{==:#{session_attached},1}", kill, "detach-client"))
    return shlex.join(("if-shell", "-F", admission, private_or_shared, rejection))
