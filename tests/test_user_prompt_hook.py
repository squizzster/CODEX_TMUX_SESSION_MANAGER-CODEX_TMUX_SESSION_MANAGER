from __future__ import annotations

import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
import yaml

import rodex.user_prompt_hook as hook_module
from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from rodex.protocol_input_text import apply_user_prompt_hook
from rodex.user_prompt_hook import USER_PROMPT_SUBSTITUTIONS_PATH, UserPromptHookConfigurationError, load_user_prompt_hook


def configured_hook(tmp_path, expressions):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(expressions), encoding="utf-8")
    return load_user_prompt_hook(path)


def prompt_request(text="Hello", *, method="turn/start", elements=None, binary=False):
    item = {"type": "text", "text": text}
    if elements is not None:
        item["text_elements"] = elements
    frame = {
        "id": "request-1",
        "method": method,
        "params": {
            "threadId": "thread-1",
            "clientUserMessageId": "dispatch-1",
            "input": [item, {"type": "localImage", "path": "/Hello.png"}],
        },
    }
    payload = json.dumps(frame)
    return InteractionRequest(
        "wire", InteractionOperation.PROTOCOL_INPUT, "codex-client", payload=payload.encode() if binary else payload
    )


def deliver(request, hook):
    received = []
    pipeline = SessionInteractionPipeline(input_text_hook=hook)
    pipeline.register(
        InteractionTarget(
            "wire",
            "runtime-1",
            frozenset({InteractionOperation.PROTOCOL_INPUT}),
            lambda: True,
            lambda item: received.append(item) or InteractionResult(DeliveryStatus.DELIVERED),
        )
    )
    result = pipeline.execute(request)
    assert result.status == DeliveryStatus.DELIVERED, result.detail
    return received[0]


@pytest.mark.parametrize("text", ["Hello", "hello", "HELLO", "hElLo"])
def test_checked_in_example_is_loaded_independently_of_caller_workspace(tmp_path, monkeypatch, text):
    # A caller workspace cannot shadow the Rodex installation's configuration.
    local_conf = tmp_path / "conf" / "hooks" / USER_PROMPT_SUBSTITUTIONS_PATH.name
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text("- '/Hello/WRONG/i'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    transformed = deliver(prompt_request(text), load_user_prompt_hook(USER_PROMPT_SUBSTITUTIONS_PATH))
    assert json.loads(transformed.payload)["params"]["input"][0]["text"] == "Hello!"


@pytest.mark.parametrize(
    "expressions,text,expected",
    [
        (["/^Hello$/Hello!/i"], "Hello there", "Hello there"),
        (["s/hello/hi/"], "hello hello", "hi hello"),
        (["s/hello/hi/gi"], "HELLO hello", "hi hi"),
        (["/^Hello$/Hi/im"], "Hello\nHello", "Hi\nHello"),
        (["/Hello.*world/done/s"], "Hello\nworld", "done"),
        (["/Hello/Hi/", "/Hi/Welcome/"], "Hello", "Welcome"),
        ([r"/^(Hello) (world)$/\2, \1!/"], "Hello world", "world, Hello!"),
        ([r"/(?P<word>Hello)/\g<word>!/"], "Hello", "Hello!"),
        ([r"/a\/b/c\/d/"], "a/b", "c/d"),
        ([r"/\\/slash/"], "a\\b", "aslashb"),
        (["/Hello//"], "Hello again", " again"),
        (["/Hello/你好/"], "Hello", "你好"),
    ],
)
def test_ordered_substitutions_flags_escapes_and_backreferences(tmp_path, expressions, text, expected):
    transformed = deliver(prompt_request(text), configured_hook(tmp_path, expressions))
    assert json.loads(transformed.payload)["params"]["input"][0]["text"] == expected


@pytest.mark.parametrize("method", ["turn/start", "turn/steer", "thread/queue/add", "thread/queue/update"])
@pytest.mark.parametrize("binary", [False, True])
def test_all_prompt_input_methods_preserve_rpc_fields_and_nontext_items(tmp_path, method, binary):
    request = prompt_request(method=method, binary=binary)
    frame = json.loads(request.payload)
    frame["params"].update({"expectedTurnId": "turn-1", "model": "Hello", "developerInstructions": "Hello"})
    frame["params"]["input"].append({"type": "text", "text": "hello"})
    encoded = json.dumps(frame)
    request = replace(request, payload=encoded.encode() if binary else encoded)
    received = deliver(request, configured_hook(tmp_path, ["/^Hello$/Hello!/i"]))
    frame["params"]["input"][0]["text"] = "Hello!"
    frame["params"]["input"][2]["text"] = "Hello!"
    assert json.loads(received.payload) == frame
    assert type(received.payload) is type(request.payload)
    assert replace(received, payload=request.payload) == request


@pytest.mark.parametrize(
    "method", ["initialize", "thread/read", "turn/interrupt", "thread/inject_items", "review/start", "goal/set"]
)
def test_native_commands_and_history_are_not_prompt_submissions(tmp_path, method):
    request = prompt_request(method=method)
    assert deliver(request, configured_hook(tmp_path, ["/Hello/Hello!/"])).payload is request.payload


@pytest.mark.parametrize(
    "operation",
    [InteractionOperation.MESSAGE, InteractionOperation.STEER, InteractionOperation.PROTOCOL_OUTPUT],
)
def test_outer_model_adapters_and_protocol_output_do_not_transform_again(tmp_path, operation):
    received = []
    # Missing config would reject any real submission; these operations never stat it.
    pipeline = SessionInteractionPipeline(input_text_hook=load_user_prompt_hook(tmp_path / "absent.yaml"))
    pipeline.register(
        InteractionTarget(
            "wire",
            "runtime",
            frozenset(InteractionOperation),
            lambda: True,
            lambda request: received.append(request) or InteractionResult(DeliveryStatus.DELIVERED),
            model_thread_id=lambda: "thread-1",
        )
    )
    request = replace(prompt_request(), operation=operation, text="Hello", expected_turn_id="turn-1")
    assert pipeline.execute(request).accepted
    assert received[0].text == "Hello"
    assert received[0].payload is request.payload


@pytest.mark.parametrize(
    "payload",
    ["not JSON", "null", "[]", '{"method":[]}', '{"method":{}}', '{"method":"turn/start","params":null}', b"\xff"],
)
def test_unrecognized_frames_stay_native(tmp_path, payload):
    request = replace(prompt_request(), payload=payload)
    assert deliver(request, configured_hook(tmp_path, ["/Hello/Hi/"])).payload is request.payload


def test_unmatched_input_preserves_exact_wire_bytes(tmp_path):
    request = prompt_request("Unchanged", binary=True)
    assert deliver(request, configured_hook(tmp_path, ["/^Hello$/Hello!/i"])).payload is request.payload


@pytest.mark.parametrize("source", ["", "# disabled\n", "[]\n"])
def test_empty_configuration_is_passthrough(tmp_path, source):
    path = tmp_path / "optional.yaml"
    if source is not None:
        path.write_text(source, encoding="utf-8")
    request = prompt_request()
    assert deliver(request, load_user_prompt_hook(path)).payload is request.payload


@pytest.mark.parametrize(
    "expression",
    [True, {}, "Hello", "/Hello/Hi", "//Hi/", "/[/Hi/", "/Hello/Hi/x", "/Hello/Hi/ii", r"/Hello/\2/", r"/Hello/\z/"],
)
def test_invalid_rule_fails_configuration_with_path_and_rule_number(tmp_path, expression):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(["/okay/first/", expression]), encoding="utf-8")
    with pytest.raises(UserPromptHookConfigurationError, match=r"rules.yaml: rule 2:"):
        load_user_prompt_hook(path)(("Hello",))


@pytest.mark.parametrize("source", ["rules: []", "false", "[not closed", "!!python/object:builtins.object {}"])
def test_invalid_yaml_fails_before_installing_any_rules(tmp_path, source):
    path = tmp_path / "rules.yaml"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(UserPromptHookConfigurationError, match=r"rules\.yaml"):
        load_user_prompt_hook(path)(("Hello",))


def test_unreadable_or_non_utf8_configuration_is_reported(tmp_path):
    with pytest.raises(UserPromptHookConfigurationError, match="cannot read user prompt hooks"):
        load_user_prompt_hook(tmp_path)(("Hello",))
    path = tmp_path / "rules.yaml"
    path.write_bytes(b"\xff")
    with pytest.raises(UserPromptHookConfigurationError, match="cannot read user prompt hooks"):
        load_user_prompt_hook(path)(("Hello",))


def test_changed_rules_apply_to_the_same_runtime_on_the_next_submission(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("- '/Hello/First/'\n", encoding="utf-8")
    existing_runtime = load_user_prompt_hook(path)
    request = prompt_request()
    assert json.loads(deliver(request, existing_runtime).payload)["params"]["input"][0]["text"] == "First"
    path.write_text("- '/Hello/Second/'\n", encoding="utf-8")
    assert json.loads(deliver(request, existing_runtime).payload)["params"]["input"][0]["text"] == "Second"


def test_text_annotations_rebase_utf8_offsets_from_exact_edits(tmp_path):
    text = "Hello @café, world"
    element = {"byteRange": {"start": 6, "end": 12}, "placeholder": "@café"}
    request = prompt_request(text, elements=[element], binary=True)
    hook = configured_hook(tmp_path, ["/Hello/你好/", "/world/earth/"])
    received = deliver(request, hook)
    item = json.loads(received.payload)["params"]["input"][0]
    assert item == {
        "type": "text",
        "text": "你好 @café, earth",
        "text_elements": [{"byteRange": {"start": 7, "end": 13}, "placeholder": "@café"}],
    }


def test_rewritten_annotation_becomes_plain_text_and_attachment_stays_intact(tmp_path):
    request = prompt_request(elements=[{"byteRange": {"start": 0, "end": 5}, "placeholder": "Hello"}])
    received = deliver(request, configured_hook(tmp_path, ["/Hello/Goodbye/"]))
    assert json.loads(received.payload)["params"]["input"] == [
        {"type": "text", "text": "Goodbye", "text_elements": []},
        {"type": "localImage", "path": "/Hello.png"},
    ]


def test_hooks_still_cannot_change_annotations_or_other_structured_metadata():
    original = prompt_request(elements=[{"byteRange": {"start": 0, "end": 5}}])
    frame = json.loads(original.payload)
    frame["params"]["input"][0]["text_elements"][0]["byteRange"]["end"] = 100
    with pytest.raises(ValueError, match="RPC identity or control fields"):
        SessionInteractionPipeline._validate_transform(original, replace(original, payload=json.dumps(frame)))


@pytest.mark.parametrize("start,end,expected", [(0, 4, []), (5, 9, [{"byteRange": {"start": 0, "end": 4}}])])
def test_repeated_text_keeps_only_the_surviving_original_annotation(tmp_path, start, end, expected):
    request = prompt_request("@foo @foo", elements=[{"byteRange": {"start": start, "end": end}}])
    received = deliver(request, configured_hook(tmp_path, ["/^@foo //"]))
    item = json.loads(received.payload)["params"]["input"][0]
    assert item["text"] == "@foo"
    assert item["text_elements"] == expected


def test_stats_only_on_submissions_and_reads_only_when_metadata_changes(tmp_path, monkeypatch):
    path = tmp_path / "rules.yaml"
    path.write_text("- '/Hello/First/'\n", encoding="utf-8")
    stats, reads = [], []
    fingerprint = hook_module.file_stat_sha512
    open_file = os.open

    def tracked_stat(path):
        stats.append(path)
        return fingerprint(path)

    def tracked_open(path, flags):
        reads.append(path)
        return open_file(path, flags)

    monkeypatch.setattr(hook_module, "file_stat_sha512", tracked_stat)
    monkeypatch.setattr(hook_module.os, "open", tracked_open)
    hook = load_user_prompt_hook(path)
    assert stats == reads == []
    unsubmitted = replace(prompt_request(), payload='{"method":"thread/read","params":{}}')
    deliver(unsubmitted, hook)
    assert stats == reads == []
    deliver(prompt_request(), hook)
    assert len(stats) == 2  # Before and after the changed-file read.
    assert reads == [path]
    deliver(prompt_request(), hook)
    assert len(stats) == 3
    assert reads == [path]
    path.write_text("- '/Hello/Second/'\n", encoding="utf-8")
    changed = deliver(prompt_request(), hook)
    assert len(stats) == 5
    assert reads == [path, path]
    assert json.loads(changed.payload)["params"]["input"][0]["text"] == "Second"


def test_atomic_replacement_with_preserved_size_and_mtime_still_reloads(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("- '/Hello/First/'\n", encoding="utf-8")
    hook = load_user_prompt_hook(path)
    deliver(prompt_request(), hook)
    previous = path.stat()
    replacement = tmp_path / "replacement.yaml"
    replacement.write_text("- '/Hello/Other/'\n", encoding="utf-8")
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_size == previous.st_size
    assert path.stat().st_mtime_ns == previous.st_mtime_ns
    changed = deliver(prompt_request(), hook)
    assert json.loads(changed.payload)["params"]["input"][0]["text"] == "Other"


def test_concurrent_submissions_share_one_complete_reload(tmp_path, monkeypatch):
    path = tmp_path / "rules.yaml"
    path.write_text("- '/Hello/First/'\n", encoding="utf-8")
    parses = []
    parse = hook_module._parse_substitutions

    def tracked_parse(path, source):
        parses.append(source)
        return parse(path, source)

    monkeypatch.setattr(hook_module, "_parse_substitutions", tracked_parse)
    hook = load_user_prompt_hook(path)
    with ThreadPoolExecutor(max_workers=4) as workers:
        received = list(workers.map(lambda _: deliver(prompt_request(), hook), range(8)))
    assert len(parses) == 1
    assert {json.loads(item.payload)["params"]["input"][0]["text"] for item in received} == {"First"}


def test_file_changed_during_read_is_retried_before_processing(tmp_path, monkeypatch):
    path = tmp_path / "rules.yaml"
    path.write_text("- '/Hello/First/'\n", encoding="utf-8")
    calls = []

    def fingerprint(_path):
        calls.append(_path)
        if len(calls) == 2:
            path.write_text("- '/Hello/Second/'\n", encoding="utf-8")
        return "old" if len(calls) == 1 else "new"

    monkeypatch.setattr(hook_module, "file_stat_sha512", fingerprint)
    received = deliver(prompt_request(), load_user_prompt_hook(path))
    assert len(calls) == 3
    assert json.loads(received.payload)["params"]["input"][0]["text"] == "Second"


def test_continuously_changing_file_has_a_bounded_error_and_can_recover(tmp_path, monkeypatch):
    path = tmp_path / "rules.yaml"
    path.write_text("[]", encoding="utf-8")
    fingerprints = iter(["one", "two", "three", "four"])
    monkeypatch.setattr(hook_module, "file_stat_sha512", lambda _path: next(fingerprints))
    hook = load_user_prompt_hook(path)
    with pytest.raises(UserPromptHookConfigurationError, match="kept changing while reading"):
        hook(("Hello",))
    monkeypatch.setattr(hook_module, "file_stat_sha512", lambda _path: "stable")
    assert hook(("Hello",)) == ((),)


@pytest.mark.parametrize("failure", [FileNotFoundError("missing"), PermissionError("denied"), OSError("unavailable")])
def test_stat_errors_are_descriptive_and_do_not_use_stale_rules(tmp_path, monkeypatch, failure):
    hook = configured_hook(tmp_path, ["/Hello/First/"])
    hook(("Hello",))

    def fail(_path):
        raise failure

    monkeypatch.setattr(hook_module, "file_stat_sha512", fail)
    with pytest.raises(UserPromptHookConfigurationError, match=r"cannot stat user prompt hooks.*rules\.yaml"):
        hook(("Hello",))


def test_fifo_is_reported_without_blocking_the_submission(tmp_path):
    path = tmp_path / "rules.yaml"
    os.mkfifo(path)
    with pytest.raises(UserPromptHookConfigurationError, match="expected a regular YAML file"):
        load_user_prompt_hook(path)(("Hello",))


def test_image_only_submission_checks_config_but_tool_output_does_not(tmp_path):
    hook = load_user_prompt_hook(tmp_path / "missing.yaml")
    image = '{"id":1,"method":"turn/start","params":{"input":[{"type":"image","url":"image"}]}}'
    with pytest.raises(UserPromptHookConfigurationError, match=r"missing\.yaml"):
        apply_user_prompt_hook(image, hook)
    tool = '{"id":2,"method":"turn/start","params":{"input":[],"toolOutput":{"output":"hello"}}}'
    assert apply_user_prompt_hook(tool, hook) is tool


def test_invalid_version_is_cached_and_its_notice_retries_until_delivered(tmp_path, monkeypatch):
    path = tmp_path / "rules.yaml"
    path.write_text("- '/[/invalid/'\n", encoding="utf-8")
    hook = load_user_prompt_hook(path)
    parses = []
    parser = hook_module._parse_substitutions
    monkeypatch.setattr(
        hook_module, "_parse_substitutions", lambda path, source: parses.append(source) or parser(path, source)
    )

    def failed_submission():
        with pytest.raises(UserPromptHookConfigurationError) as raised:
            hook(("Hello",))
        return raised.value

    first = failed_submission()
    assert not first.notice.notify(lambda: False)
    second = failed_submission()
    notices = []
    assert second.notice.notify(lambda: notices.append("displayed") or True)
    assert failed_submission().notice.notify(lambda: notices.append("duplicate") or True)
    assert len(parses) == 1
    assert notices == ["displayed"]


def test_missing_file_notice_resets_after_a_successful_stat(tmp_path):
    path = tmp_path / "rules.yaml"
    hook = load_user_prompt_hook(path)
    notices = []

    def fail_and_notify():
        with pytest.raises(UserPromptHookConfigurationError) as raised:
            hook(("Hello",))
        raised.value.notice.notify(lambda: notices.append(str(raised.value)) or True)

    fail_and_notify()
    fail_and_notify()
    assert len(notices) == 1
    path.write_text("[]", encoding="utf-8")
    assert hook(("Hello",)) == ((),)
    path.unlink()
    fail_and_notify()
    assert len(notices) == 2


def test_identical_stat_hash_never_reads_contents_even_if_bytes_changed(tmp_path, monkeypatch):
    # The user's detector intentionally has metadata-only semantics.
    monkeypatch.setattr(hook_module, "file_stat_sha512", lambda _path: "same-metadata")
    path = tmp_path / "rules.yaml"
    path.write_text("- '/Hello/First/'\n", encoding="utf-8")
    hook = load_user_prompt_hook(path)
    deliver(prompt_request(), hook)
    path.write_text("- '/Hello/Second/'\n", encoding="utf-8")
    unchanged = deliver(prompt_request(), hook)
    assert json.loads(unchanged.payload)["params"]["input"][0]["text"] == "First"


PUSH_EXPANSION = (
    "**Commit/stash → Fetch/prune → Switch main → Pull → Find unmerged branches → Merge all → "
    "Resolve conflicts → Push → Verify → Delete merged branches → Final status**"
)


@pytest.mark.parametrize("text", ["push", "PUSH", "  Push!\t", "\tpush.!?,;:\t"])
def test_user_supplied_named_push_rule_expands_submitted_text(text):
    received = deliver(prompt_request(text), load_user_prompt_hook(USER_PROMPT_SUBSTITUTIONS_PATH))
    assert json.loads(received.payload)["params"]["input"][0]["text"] == PUSH_EXPANSION


def test_user_supplied_push_rule_preserves_multiline_and_crlf_boundaries():
    text = "push\nPUSH!\r\nother text\nHello"
    received = deliver(prompt_request(text), load_user_prompt_hook(USER_PROMPT_SUBSTITUTIONS_PATH))
    assert json.loads(received.payload)["params"]["input"][0]["text"] == (
        f"{PUSH_EXPANSION}\n{PUSH_EXPANSION}\r\nother text\nHello"
    )


@pytest.mark.parametrize("text", ["push code", "please push", "pushing", "push x", "Hello there"])
def test_user_supplied_named_rules_leave_nonmatches_unchanged(text):
    request = prompt_request(text)
    assert deliver(request, load_user_prompt_hook(USER_PROMPT_SUBSTITUTIONS_PATH)).payload is request.payload


def test_named_rules_share_order_flags_backreferences_and_do_not_match_the_name(tmp_path):
    hook = configured_hook(
        tmp_path,
        [
            {"name": "Label, not a pattern", "match": r"^(hello) (world)$", "replace": r"\2, \1!", "flags": "i"},
            {"name": "Decorate", "match": "world", "replace": "earth"},
        ],
    )
    result = deliver(prompt_request("Hello world"), hook)
    assert json.loads(result.payload)["params"]["input"][0]["text"] == "earth, Hello!"
    request = prompt_request("Label, not a pattern")
    assert deliver(request, hook).payload is request.payload


@pytest.mark.parametrize(
    "changed",
    [
        {"match": None},
        {"replace": False},
        {"flags": []},
        {"flags": "x"},
        {"name": ""},
        {"name": False},
        {"match": "["},
        {"replace": r"\9"},
        {"typo": "unused"},
    ],
)
def test_invalid_named_rule_reports_its_file_position_and_label(tmp_path, changed):
    hook = configured_hook(
        tmp_path, [{"name": "Greeting", "match": "Hello", "replace": "Hello!", "flags": "i"} | changed]
    )
    with pytest.raises(UserPromptHookConfigurationError, match=r"rules\.yaml: rule 1") as raised:
        hook(("Hello",))
    if "name" not in changed:
        assert "Greeting" in str(raised.value)


@pytest.mark.parametrize("missing", ["name", "match", "replace"])
def test_missing_named_rule_fields_are_reported(tmp_path, missing):
    rule = {"name": "Greeting", "match": "Hello", "replace": "Hello!"}
    del rule[missing]
    hook = configured_hook(tmp_path, [rule])
    with pytest.raises(UserPromptHookConfigurationError, match=f"missing rule fields: {missing}"):
        hook(("Hello",))


def write_named_rules(path, *rules):
    path.write_text(
        yaml.safe_dump(
            [{"name": name, "match": pattern, "replace": replacement} for name, pattern, replacement in rules]
        ),
        encoding="utf-8",
    )


def submitted_text(hook, text="Hello"):
    return json.loads(deliver(prompt_request(text), hook).payload)["params"]["input"][0]["text"]


@pytest.mark.parametrize("override", [False, True])
def test_default_hook_uses_callers_state_home_independently_of_daemon_environment(tmp_path, monkeypatch, override):
    monkeypatch.setenv("HOME", str(tmp_path / "daemon-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "daemon-state"))
    environment = {"HOME": str(tmp_path / "caller-home")}
    root = tmp_path / "caller-home" / ".local" / "state"
    if override:
        root = tmp_path / "caller-state"
        environment["XDG_STATE_HOME"] = str(root)
    path = root / "rodex" / "conf" / "hooks" / "user_prompt_substitutions.yaml"
    assert hook_module.default_user_prompt_substitutions_path(environment) == path
    path.parent.mkdir(parents=True)
    write_named_rules(path, ("Add enthusiasm to Hello", "^Hello$", "Personal greeting"))
    assert submitted_text(load_user_prompt_hook(environment=environment)) == "Personal greeting"


def test_user_rules_replace_by_name_at_global_position_and_append_new_rules(tmp_path):
    global_path, user_path = tmp_path / "global.yaml", tmp_path / "user.yaml"
    write_named_rules(global_path, ("Greeting", "^Hello$", "Global"), ("Decorate", "^Personal$", "Decorated"))
    write_named_rules(user_path, ("New", "^Decorated$", "Final"), ("Greeting", "^Hello$", "Personal"))
    hook = load_user_prompt_hook(global_path, user_path=user_path)
    assert submitted_text(hook) == "Final"


def test_unnamed_shorthand_rules_in_both_files_are_applied_in_order(tmp_path):
    global_path, user_path = tmp_path / "global.yaml", tmp_path / "user.yaml"
    global_path.write_text("- '/Hello/Global/'\n", encoding="utf-8")
    user_path.write_text("- '/Global/Personal/'\n", encoding="utf-8")
    assert submitted_text(load_user_prompt_hook(global_path, user_path=user_path)) == "Personal"


def test_each_file_is_statted_on_submit_but_only_changed_file_is_read(tmp_path, monkeypatch):
    global_path, user_path = tmp_path / "global.yaml", tmp_path / "user.yaml"
    write_named_rules(global_path, ("Greeting", "^Hello$", "Global"))
    write_named_rules(user_path, ("Greeting", "^Hello$", "Personal"))
    stats, reads = Counter(), Counter()
    fingerprint, open_file = hook_module.file_stat_sha512, os.open

    def tracked_stat(path):
        stats[os.fspath(path)] += 1
        return fingerprint(path)

    def tracked_open(path, flags):
        reads[os.fspath(path)] += 1
        return open_file(path, flags)

    monkeypatch.setattr(hook_module, "file_stat_sha512", tracked_stat)
    monkeypatch.setattr(hook_module.os, "open", tracked_open)
    hook = load_user_prompt_hook(global_path, user_path=user_path)
    assert not stats and not reads
    assert submitted_text(hook) == submitted_text(hook) == "Personal"
    assert stats == {str(global_path): 3, str(user_path): 3}
    assert reads == {str(global_path): 1, str(user_path): 1}
    write_named_rules(user_path, ("Greeting", "^Hello$", "Changed"))
    assert submitted_text(hook) == "Changed"
    assert stats == {str(global_path): 4, str(user_path): 5}
    assert reads == {str(global_path): 1, str(user_path): 2}
    write_named_rules(global_path, ("Greeting", "^Hello$", "Global"), ("Suffix", "^Changed$", "Changed!"))
    assert submitted_text(hook) == "Changed!"
    assert stats == {str(global_path): 6, str(user_path): 6}
    assert reads == {str(global_path): 2, str(user_path): 2}


def test_optional_user_file_creation_emptying_removal_and_reappearance_reload(tmp_path):
    global_path, user_path = tmp_path / "global.yaml", tmp_path / "user.yaml"
    write_named_rules(global_path, ("Greeting", "^Hello$", "Global"))
    hook = load_user_prompt_hook(global_path, user_path=user_path)
    assert submitted_text(hook) == "Global"
    write_named_rules(user_path, ("Greeting", "^Hello$", "Personal"))
    assert submitted_text(hook) == "Personal"
    user_path.write_text("[]", encoding="utf-8")
    assert submitted_text(hook) == "Global"
    write_named_rules(user_path, ("Greeting", "^Hello$", "Restored"))
    assert submitted_text(hook) == "Restored"
    user_path.unlink()
    assert submitted_text(hook) == "Global"
    write_named_rules(user_path, ("Greeting", "^Hello$", "Reappeared"))
    assert submitted_text(hook) == "Reappeared"


def test_user_file_deleted_during_reload_immediately_restores_global_rules(tmp_path, monkeypatch):
    global_path, user_path = tmp_path / "global.yaml", tmp_path / "user.yaml"
    write_named_rules(global_path, ("Greeting", "^Hello$", "Global"))
    write_named_rules(user_path, ("Greeting", "^Hello$", "Personal"))
    fingerprint = hook_module.file_stat_sha512
    user_stats = 0

    def delete_during_read(path):
        nonlocal user_stats
        if path == str(user_path):
            user_stats += 1
            if user_stats == 2:
                user_path.unlink()
        return fingerprint(path)

    monkeypatch.setattr(hook_module, "file_stat_sha512", delete_during_read)
    assert submitted_text(load_user_prompt_hook(global_path, user_path=user_path)) == "Global"


@pytest.mark.parametrize("reappears", [False, True])
def test_optional_file_disappearing_between_stat_and_open_uses_current_state(tmp_path, monkeypatch, reappears):
    global_path, user_path = tmp_path / "global.yaml", tmp_path / "user.yaml"
    write_named_rules(global_path, ("Greeting", "^Hello$", "Global"))
    write_named_rules(user_path, ("Greeting", "^Hello$", "Personal"))
    open_file = os.open
    interrupted = False

    def disappear_before_open(path, flags):
        nonlocal interrupted
        if path == user_path and not interrupted:
            interrupted = True
            user_path.unlink()
            if reappears:
                write_named_rules(user_path, ("Greeting", "^Hello$", "Replacement"))
            raise FileNotFoundError("optional user file disappeared before open")
        return open_file(path, flags)

    monkeypatch.setattr(hook_module.os, "open", disappear_before_open)
    hook = load_user_prompt_hook(global_path, user_path=user_path)
    assert submitted_text(hook) == ("Replacement" if reappears else "Global")


def test_each_invalid_file_retains_its_own_cached_error_notice(tmp_path, monkeypatch):
    global_path, user_path = tmp_path / "global.yaml", tmp_path / "user.yaml"
    for path in (global_path, user_path):
        path.write_text("[unclosed", encoding="utf-8")
    parses = Counter()
    parser = hook_module._parse_substitutions

    def counted_parse(path, content):
        parses[path] += 1
        return parser(path, content)

    monkeypatch.setattr(hook_module, "_parse_substitutions", counted_parse)
    hook = load_user_prompt_hook(global_path, user_path=user_path)
    notices = []

    def notify_errors():
        with pytest.raises(UserPromptHookConfigurationError) as raised:
            hook(("Hello",))
        for error in raised.value.errors or (raised.value,):
            error.notice.notify(lambda error=error: notices.append(str(error)) or True)

    notify_errors()
    notify_errors()
    assert len(notices) == 2
    assert parses == {global_path: 1, user_path: 1}
    assert str(global_path) in notices[0] and str(user_path) in notices[1]
    user_path.write_text("- '/Hello/invalid/x'", encoding="utf-8")
    notify_errors()
    assert len(notices) == 3 and str(user_path) in notices[-1]
    assert parses == {global_path: 1, user_path: 2}
    write_named_rules(global_path, ("Greeting", "^Hello$", "Global"))
    user_path.unlink()
    assert submitted_text(hook) == "Global"
    user_path.write_text("[unclosed", encoding="utf-8")
    notify_errors()
    assert len(notices) == 4 and str(user_path) in notices[-1]


@pytest.mark.parametrize("failure", [PermissionError("denied"), NotADirectoryError("bad parent")])
def test_optional_user_file_stat_failures_other_than_absence_are_reported(tmp_path, monkeypatch, failure):
    global_path, user_path = tmp_path / "global.yaml", tmp_path / "user.yaml"
    global_path.write_text("[]", encoding="utf-8")
    fingerprint = hook_module.file_stat_sha512

    def fail_user_stat(path):
        if path == str(user_path):
            raise failure
        return fingerprint(path)

    monkeypatch.setattr(hook_module, "file_stat_sha512", fail_user_stat)
    with pytest.raises(UserPromptHookConfigurationError, match=r"user\.yaml"):
        load_user_prompt_hook(global_path, user_path=user_path)(("Hello",))


def test_duplicate_names_within_one_file_are_ambiguous_and_rejected(tmp_path):
    path = tmp_path / "rules.yaml"
    write_named_rules(path, ("Greeting", "Hello", "First"), ("Greeting", "Hello", "Second"))
    with pytest.raises(UserPromptHookConfigurationError, match=r"rule 2.*duplicate rule name: Greeting"):
        load_user_prompt_hook(path)(("Hello",))


def test_yaml_escaped_surrogate_is_reported_as_config_error_before_delivery(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text('- name: Greeting\n  match: Hello\n  replace: "\\uD800"\n', encoding="utf-8")
    with pytest.raises(UserPromptHookConfigurationError, match=r"rules\.yaml: rule 1.*surrogates not allowed"):
        load_user_prompt_hook(path)(("Hello",))
