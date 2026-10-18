"""Wake-up rules that agents manage themselves (owner corrections, 10 October 2026).

An agent creates, reads, edits, pauses, resumes and deletes its own wake-up rules with its Fleet tools, and the
fleet manager (the team's one "manager") does it for every agent, itself included. A rule is a trigger of owner API
1.7 and 1.8: the hub's scheduler, runs, admission, helper checks and transactions stay as they are, and there is no
second scheduler. This module holds only the new decisions. It has no database access and does not import the hub,
so its tests need no hub:

- authority: the hub's one authority function, authority(actor, target, action), decides every rule write, every
  read and every wake, and every task that a wake starts;
- the route of a wake-up: one inbox message for the fleet manager, or a task whose requester can see its result: the
  fleet manager directly, or a lead through a parent task in its own task session;
- saved check code: an agent writes a check as text; the hub keeps each version by its SHA-256 and never changes it,
  without a repository commit or an owner pin for each change;
- where saved code runs (machine placement, apart from the logical team): only on an agent with the execution
  grant (execution_grant: its existing runtime already runs any command without asking, and every restriction that
  the runtime still applies is applied to the check too), and that the code's author has authority over. The same
  rule holds for every runtime; Claude Code does not qualify yet (see execution_grant);
- authority is always the current one: a rule is checked again at each send, each claim, each result and each
  prompt run (still_valid), with its author's current authority, so a reporting-line or grant change makes a
  stale rule or claim start nothing. A check result is data: it never changes a rule or an authority;
- the persisted identity of each write (command_identity), so that a repeated tool call gets its first receipt;
- the texts and the input schemas of the agent tools.

The hub passes facts in, applies each returned plan with its own trigger functions inside one transaction, and maps
Refused to its ToolError. Standard library only.
"""
import hashlib
import json
import re
import shlex

CODE_MAX = 65536            # bytes of one saved check: the limit of a repository check (owner API 1.8)
RULES_MAX = 20              # rules that agents set for one target and that are not deleted
ACTIONS = ("pause", "resume", "delete", "check_now")
RULE_FIELDS = ("agent", "schedule", "task", "late_minutes", "max_tasks_per_day")    # the hub checks them (1.7)
SAVE_KEYS = {"request_id", "trigger_id", "base_revision", "check", "task_id"} | set(RULE_FIELDS)
CHANGE_KEYS = {"request_id", "trigger_id", "base_revision", "action", "task_id"}    # call_tool adds task_id
CHECK_KEYS = {"code", "run_on", "args", "timeout_seconds", "condition", "repeat_minutes"}
SAVED = ("code", "sha256", "bytes")       # the fields of a stored saved check that come from its code
REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,63}")
DENY_PATHS = "check_deny_paths"     # helper feature: it applies Fleet's deny list (fleet.py DENY, PROTECTED) to checks
OWNER, FLEET, SELF, NONE = 3, 2, 1, 0       # levels; OWNER is only the owner's own rules
OPS = {">": "more than", ">=": "at least", "<": "less than", "<=": "at most", "==": "equal to", "!=": "not equal to"}


class Refused(Exception):
    """A request that the caller may not make, or that is not valid. The hub answers it as a tool error."""


class NeedsCodeApproval(Refused):
    """A save of saved code that is valid in every point except one: its author has no authority over the runner,
    which has the execution grant. `subject` (code_subject) is the exact revision that the requester of the author's
    task can approve with a code prompt; after an allow, the hub calls plan_save again with the approval. Without
    that, it is a refusal like any other, and nothing is saved."""

    def __init__(self, message, subject):
        super().__init__(message)
        self.subject = subject


class Org:
    """What the module reads from the hub: the fleet manager's name, the team.json agents, the hub's one authority
    function, authority(actor, target, action) -> (allowed, reason), and the features
    that each helper reports. The module never decides authority itself: it asks this function, so the rules of the
    module and of the hub cannot drift apart."""

    def __init__(self, manager, agents, authority, features=None):
        self.manager, self.agents, self.authority = manager, agents, authority
        self.features = features or (lambda agent: ())     # hub.helper_features: what an agent's helper reports


def team_authority(manager, agents):
    """A stand-in with the rules of hub.authority, for the tests of this module."""
    def authority(actor, target, action):
        if actor not in agents:
            return False, f"{actor} is not an agent of this team."
        if action == "rule":
            if target not in agents:
                return False, f"There is no agent {target} in the team."
            if actor in (manager, target):
                return True, None
            return False, f"Only {target} itself or {manager} sets {target}'s rules."
        up = (agents.get(target) or {}).get("reports_to") or manager
        if (agents.get(target) or {}).get("role") not in ("worker", "lead") or up != actor:
            return False, f"'{target}' does not report to you."
        if (agents.get(target) or {}).get("active", True) is False:
            return False, f"{target} is in setup. The owner activates it in Fleet."
        return True, None
    return authority


def level(org, caller, target):
    """The caller's level over `target`'s rules, from the one authority function: FLEET for the fleet manager, SELF
    for the target itself, NONE when not allowed."""
    if not org.authority(caller, target, "rule")[0]:
        return NONE
    return FLEET if caller == org.manager else SELF


def boss(org, name):
    """The agent that gives `name` its tasks in the reporting lines: its lead, or the fleet manager. None for the
    fleet manager. As hub.reports_to."""
    conf = org.agents.get(name) or {}
    if name == org.manager or conf.get("role") == "manager":
        return None
    up = conf.get("reports_to")
    return up if up in org.agents and up != name else org.manager


# ---------------------------------------------------------------- authority over rules

def creator_rank(org, rule):
    """The rank that a rule needs from its author: OWNER for the owner's rules, SELF for an agent's rule for itself,
    and FLEET for every other author, which is the fleet manager (and an older author that is neither, which only the
    fleet manager takes over). The author (created_by) is the agent that saved the rule last: an agent's accepted edit
    makes the editor the author, and the history keeps every author. An owner edit keeps the author."""
    by = rule.get("created_by") or "owner"
    return OWNER if by == "owner" else SELF if by == rule["agent"] and by != org.manager else FLEET


def may_read(org, caller, rule):
    """By current authority only: the fleet manager sees every rule, and an agent the rules that wake it. An agent
    that left the team, and an author that lost its authority, see nothing (the source and history can hold the
    team's own data)."""
    return caller == org.manager or level(org, caller, rule["agent"]) >= SELF


def may_control(org, caller, rule, action=None):
    """None, or Refused: may `caller` edit `rule`, or apply `action` to it? Agents never change the owner's rules. The
    fleet manager changes every agent's rule. Otherwise the caller needs, now, at least the rank that the rule needs
    from its author: so an agent changes its own rule for itself but not what the fleet manager set for it. Only the
    owner resumes a rule that the owner paused."""
    tid = rule["id"]
    if not may_read(org, caller, rule):
        raise Refused(f"Trigger {tid} is not yours.")
    need = creator_rank(org, rule)
    if need == OWNER:
        raise Refused(f"The owner set trigger {tid}. Only the owner changes it, in Fleet. Tell your requester what "
                      f"should change.")
    if caller != org.manager and level(org, caller, rule["agent"]) < max(SELF, need):
        raise Refused(f"{rule.get('created_by')} set trigger {tid}. You cannot change it.")
    if action == "resume":
        rank = pause_rank(org, rule)
        if rank == OWNER:
            raise Refused(f"The owner paused trigger {tid}. Only the owner resumes it.")
        if caller != org.manager and level(org, caller, rule["agent"]) < rank:
            raise Refused(f"{rule.get('paused_by')} paused trigger {tid}. Only the fleet manager resumes it.")


def pause_rank(org, rule):
    """The rank that a resume needs, from who paused the rule (paused_by), as creator_rank does for an edit: OWNER for
    the owner's pause (and for a paused rule without a pauser), SELF for the target's own pause, NONE for the hub's
    pause (anyone who may change the rule resumes it once still_valid passes), and FLEET for every other pauser. So
    an agent never resumes what the fleet manager paused."""
    by = rule.get("paused_by")
    if by == "hub":
        return NONE
    if by == "owner" or (by is None and rule.get("state") == "paused"):
        return OWNER
    if by is None:
        return NONE
    return SELF if by == rule["agent"] and by != org.manager else FLEET


def check_set(org, caller, target):
    """None, or Refused with the hub's own reason: may `caller` set a rule for `target`?"""
    if target not in org.agents:
        raise Refused(f"There is no agent {target} in the team.")
    allowed, why = org.authority(caller, target, "rule")
    if not allowed:
        raise Refused(why)


def wake_route(org, target):
    """(route, None), or (None, why) when a match cannot start anything that someone can follow. A wake-up of the
    fleet manager is one inbox message. Otherwise it is a task for the target, whose requester is the target's boss in
    the reporting lines:

    {"kind": "message", "to": <fleet manager>}
    {"kind": "task", "owner": <target>, "requester": <fleet manager>, "parent": None}
    {"kind": "task", "owner": <target>, "requester": <R>, "parent": {"owner": <R>, "requester": <fleet manager>}}

    A requester other than the fleet manager reads results only in its own task sessions, so the hub starts a parent
    task for it in the same transaction, and the target's task is its subtask. That requester must be a lead (it needs
    the team tools) that reports to the fleet manager. Every task of the route must pass the one authority function
    (action "task"), so a wake never starts a task that its requester could not give."""
    if target == org.manager:
        return {"kind": "message", "to": target}, None
    requester = boss(org, target)
    parent = None
    if requester != org.manager:
        conf = org.agents.get(requester) or {}
        if conf.get("role") != "lead":
            return None, (f"{requester} would receive {target}'s result, but it is not a lead, so it has no tools to "
                          f"check results. The owner can make it a lead.")
        if boss(org, requester) != org.manager:
            return None, (f"{requester} would receive {target}'s result, but it reports to {boss(org, requester)}, "
                          f"not to the manager. A wake-up can go through one lead only.")
        parent = {"owner": requester, "requester": org.manager}
        allowed, why = org.authority(org.manager, requester, "task")
        if not allowed:
            return None, why
    allowed, why = org.authority(requester, target, "task")
    if not allowed:
        return None, why
    return {"kind": "task", "owner": target, "requester": requester, "parent": parent}, None


# ---------------------------------------------------------------- saved code and where it runs (machine placement)

def flag(words, *names):
    """The value of a command-line option, "--sandbox x" or "--sandbox=x", or None."""
    for i, word in enumerate(words):
        for name in names:
            if word == name and i + 1 < len(words):
                return words[i + 1]
            if name.startswith("--") and word.startswith(name + "="):
                return word[len(name) + 1:]
    return None


def option_values(words, *names):
    """Every value of a repeatable option ("--allowedTools a b" stops at the next option, or "--allowedTools=a,b")."""
    out = []
    for i, word in enumerate(words):
        for name in names:
            if word == name:
                for value in words[i + 1:]:
                    if value.startswith("-"):
                        break
                    out += [v for v in value.replace(",", " ").split() if v]
            elif word.startswith(name + "="):
                out += [v for v in word[len(name) + 1:].replace(",", " ").split() if v]
    return out


def execution_grant(conf, features=()):
    """(granted, why): the narrow execution grant for saved code, one rule for every runtime. Saved code runs as the
    runner's machine user, outside every AI session, so it may run only where it adds no access: the runner's existing
    runtime grant (its start command and allow list in team.json) already runs any command without asking, and every
    restriction that this runtime still applies is applied to the check as well. There is no new grant, no inference
    from the reporting lines and no approval of each code version.

    - Codex: --sandbox danger-full-access with --ask-for-approval never, or its bypass flag, and no -c override of the
      sandbox or the approvals. No restriction is left, so it is granted.
    - Claude Code: --dangerously-skip-permissions, --permission-mode bypassPermissions, or Bash allowed without a
      pattern. Fleet's deny list (fleet.py DENY and PROTECTED) still applies to a worker's or a lead's runtime, so it is
      granted only when the runner's helper reports the feature check_deny_paths: it applies the same deny list to
      each check, with the operating system. No helper reports it yet. The fleet manager's sessions have no Fleet deny
      list (owner correction, 13 October 2026), so its permission bypass is granted without that feature.
    - Everything else asks before commands, or is not known: not granted."""
    try:
        words = shlex.split(conf.get("start") or "")
    except ValueError:
        return False, "Its start command in team.json cannot be read."
    runtime = conf.get("runtime")
    if runtime == "codex":
        configs = option_values(words, "-c", "--config")
        if any(c.split("=", 1)[0].strip().startswith(("sandbox", "approval")) for c in configs):
            return False, "Its Codex start command changes the sandbox or the approvals with -c, which Fleet does not read."
        if "--dangerously-bypass-approvals-and-sandbox" in words or "--yolo" in words or (
                flag(words, "--sandbox", "-s") == "danger-full-access" and
                flag(words, "--ask-for-approval", "-a") == "never"):
            return True, "Codex already runs any command without asking, with no sandbox."
        return False, ("Its Codex runtime asks before commands or uses a sandbox. The grant needs --sandbox "
                       "danger-full-access and --ask-for-approval never.")
    if runtime == "claude":
        allow = list(conf.get("allow") or []) + option_values(words, "--allowedTools", "--allowed-tools")
        bypass = "--dangerously-skip-permissions" in words or flag(words, "--permission-mode") == "bypassPermissions"
        # fleet.py settings_arg gives the manager no deny rule. Its start must keep every built-in tool: one that
        # removes a tool, such as Bash, would let a check run what the session itself cannot.
        every_tool = flag(words, "--tools") in (None, "default") and \
            not option_values(words, "--disallowedTools", "--disallowed-tools")
        if bypass and conf.get("role") == "manager" and every_tool:
            return True, "Claude Code already runs any command without asking, and Fleet adds no deny rule to it."
        if not (bypass or any(a in ("Bash", "Bash(*)", "Bash(*:*)") for a in allow)):
            return False, (f"Its Claude Code runtime asks before commands (permission mode "
                           f"{flag(words, '--permission-mode') or 'default'}).")
        if DENY_PATHS not in features:
            return False, ("Its Claude Code runtime runs commands without asking, but Fleet's deny list still applies to "
                           "it, and its helper does not apply that list to checks yet (helper feature "
                           "check_deny_paths). Claude Code checks are not supported yet.")
        return True, "Claude Code already runs any command without asking, and the helper applies Fleet's deny list."
    return False, f"There is no known execution grant for the runtime {runtime}."


def runner_of(rule_or_target, check):
    """The agent whose helper runs the check: check.run_on, or the rule's target."""
    target = rule_or_target["agent"] if isinstance(rule_or_target, dict) else rule_or_target
    return (check or {}).get("run_on") or target


def code_subject(target, check):
    """The exact revision that a code approval covers: the code's author, the rule's target, the runner, the SHA-256
    of the code and of its args, and its time limit. Any other change keeps the approval; a change of one of these
    needs a new one."""
    args = check.get("args")
    canonical = json.dumps({} if args is None else args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    timeout = check.get("timeout_seconds")
    return {"author": check.get("code_by"), "target": target, "runner": runner_of(target, check),
            "code_sha256": check.get("sha256"), "args_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "timeout_seconds": 30 if timeout is None else timeout}


def runner_problem(org, runner, author):
    """Why `runner` cannot run saved code at all, or None: it is not in the team, is in setup, or has no execution
    grant. An approval never changes this."""
    conf = org.agents.get(runner)
    if conf is None:
        return f"There is no agent {runner} in the team to run the check."
    if conf.get("active", True) is False:
        return f"{runner} is in setup and cannot run checks."
    granted, why = execution_grant(conf, org.features(runner))
    if not granted:
        return (f"{runner} cannot run saved code: {why} Saved code runs only on an agent with the execution grant "
                f"that you control. {runner_hint(org, author)}")
    return None


def approval_holds(org, author, runner, approval, subject):
    """(True, None), or (False, why): a code approval that the hub stored ({"prompt", "approver", "subject"}) covers
    this exact revision now. Its subject must equal this one, and its approver must still give the author work and
    still control the runner. A failed approval is never renewed here: the author saves the check again, and its
    current requester decides."""
    again = "Save the check again, so that the requester of your task can decide it."
    if not isinstance(approval, dict) or subject is None or approval.get("subject") != subject:
        return False, f"The code approval does not cover this code, its args, its runner or its time limit. {again}"
    approver = approval.get("approver")
    if approver not in org.agents:
        return False, f"{approver}, who approved this code, is no longer in the team. {again}"
    if not org.authority(approver, author, "task")[0]:
        return False, f"{approver} approved this code and no longer gives {author} work. {again}"
    if level(org, approver, runner) < SELF:
        return False, f"{approver} approved this code and no longer controls {runner}. {again}"
    return True, None


def code_grant(org, author, runner, approval=None, subject=None):
    """(allowed, reason): may code that `author` saved run as a check on `runner`'s machine? The code runs as that
    machine's user, outside every AI session, so two things must hold:

    1. the runner has the execution grant (execution_grant), with the features that its helper reports now;
    2. the author may set rules for the runner now (the one authority function), or a code approval (approval_holds)
       covers this exact revision (`subject`): its approver gives the author work and controls the runner now. The
       author gets no authority over the runner from it.

    Routine changes of the code inside this grant need no new approval. If several agents share one machine user
    later, the grant must hold for that user, because the code gets the access of the user."""
    problem = runner_problem(org, runner, author)
    if problem:
        return False, problem
    if level(org, author, runner) >= SELF:
        return True, None
    if approval is not None:
        return approval_holds(org, author, runner, approval, subject)
    return False, (f"{author} has no authority over {runner}, so the check cannot run there. "
                   f"{runner_hint(org, author)}")


def eligible_runners(org, author):
    """The agents that `author` can name in check.run_on now: active, with the execution grant (with the features
    that their helpers report now), and under the author's existing authority. The module only names them: it never
    selects one, and the list gives no authority."""
    return sorted(name for name, conf in org.agents.items()
                  if conf.get("active", True) is not False and level(org, author, name) >= SELF
                  and execution_grant(conf, org.features(name))[0])


def runner_hint(org, author):
    """The end of a refusal of saved code: the runners that the author already controls, or what it can do without
    one. Asking for a check gives the asker no code access: the check and its code stay the author's."""
    names = eligible_runners(org, author)
    if names:
        return f"Runners that you control: {', '.join(names)}. Name one in check.run_on."
    text = "You control no runner with the execution grant."
    if author != org.manager:
        text += (" In a task session, you can name in check.run_on a runner that the requester of your task controls: "
                 "that requester then decides this exact code revision, and each new revision needs its decision "
                 "again. You get no code access of your own.")
    return text


def saved_check(value, author, old=None):
    """(stored check, source): an agent's check, with its code kept by SHA-256. A stored version never changes: new
    code is a new SHA-256 and a new trigger revision. In an edit, a check without "code" keeps the code of `old` (the
    stored check), so a new condition, new args or a new runner need no resend; source is then None. The caller
    becomes code_by, the agent that answers for where the code runs. The hub checks args, timeout_seconds, condition
    and repeat_minutes with its 1.8 rules. An agent cannot pin a repository check: path and commit are not agent
    fields."""
    if not isinstance(value, dict):
        raise Refused("check must be an object: code, condition and repeat_minutes, and optional run_on, args and "
                      "timeout_seconds. Use null to remove the check.")
    extra = sorted(set(value) - CHECK_KEYS)
    if extra:
        raise Refused(f"check has no field {extra[0]}. Its fields are code, condition, repeat_minutes, run_on, args "
                      f"and timeout_seconds.")
    rest = {k: v for k, v in value.items() if k not in ("code", "run_on")}
    run_on = value.get("run_on")
    if run_on is not None and not isinstance(run_on, str):
        raise Refused("check.run_on must be an agent name, or absent for the rule's agent.")
    if "code" not in value:
        if not old or old.get("code") != "saved":
            raise Refused("check.code is required: the text of the Python program that the check runs.")
        return dict(rest, **{k: old[k] for k in SAVED}, code_by=author, run_on=run_on), None
    code = value["code"]
    if not isinstance(code, str) or not code.strip():
        raise Refused("check.code must be the text of a Python program.")
    if "\x00" in code:
        raise Refused("check.code must not contain a NUL character.")
    try:
        data = code.encode("utf-8")
    except UnicodeEncodeError:
        raise Refused("check.code must be valid Unicode text.") from None
    if len(data) > CODE_MAX:
        raise Refused(f"check.code has {len(data)} bytes; the limit is {CODE_MAX} bytes (64 KiB).")
    return dict(rest, code="saved", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), code_by=author,
                run_on=run_on), code


def still_valid(org, rule):
    """(True, None), or (False, why), with the current team and authority. The hub asks before each send of a check,
    at each claim, before each decision of a result and at each prompt run, so a rule or a claim that became stale
    starts nothing: the author lost its authority, the code's author lost authority over the runner, the runner lost
    its grant, or the target went into setup. The run then ends without a task or a message, and the hub pauses the
    rule with the reason. The owner's own rules keep the hub's 1.7 and 1.8 rules."""
    target = rule["agent"]
    if target not in org.agents:
        return False, f"{target} is no longer in the team."
    if org.agents[target].get("active", True) is False:
        return False, f"{target} is in setup."
    by = rule.get("created_by") or "owner"
    if by == "owner":
        return True, None
    need = creator_rank(org, rule)
    if need != SELF and level(org, by, target) < need:
        return False, (f"{by} set this rule and no longer has authority over {target}. The fleet manager can save it "
                       f"again to take it over.")
    route, why = wake_route(org, target)
    if route is None:
        return False, why
    check = rule.get("check")
    if check and check.get("code") == "saved":
        # A stored approval counts only while it covers this revision and its approver still has the authority.
        allowed, why = code_grant(org, check.get("code_by"), runner_of(rule, check), check.get("approval"),
                                  code_subject(target, check))
        if not allowed:
            return False, why
    return True, None


# ---------------------------------------------------------------- plans for the agent tools

def trigger_ref(value):
    """A trigger number from 7 or "7"."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    if isinstance(value, str) and value.strip().isdigit() and int(value) >= 1:
        return int(value)
    raise Refused("trigger_id must be a trigger number, for example 7.")


def revision_of(args):
    base = args.get("base_revision")
    if isinstance(base, bool) or not isinstance(base, int) or base < 1:
        raise Refused("base_revision is required: the revision that see_triggers shows for the trigger.")
    return base


def plan_save(org, caller, args, rule=None, rules=(), approval=None):
    """The plan of one save_trigger call, or Refused. The hub then checks the 1.7 fields with trigger_fields and the
    1.8 check fields with trigger_check_spec, and applies the plan in one transaction, with a trigger_history row.

    rule: the stored rule of args["trigger_id"] for an edit, or None when there is no such trigger.
    rules: the stored rules that are not deleted, for RULES_MAX (all targets; the count filters by target).

    Returns {"op": "create" or "edit", "trigger_id", "base_revision", "fields", "route", "source", "by"},
    and "check" when the save sets the check. fields has the 1.7 fields (an edit only the given ones). check is None
    (no check, or remove it) or the stored saved check; without the key, an edit keeps the stored check. source is
    the code text to keep by its SHA-256, or None when the code is not new. by becomes the rule's author
    (created_by), also in an edit. The hub first looks up command_identity(caller, "save_trigger", args).

    approval: a code approval that the hub stored ({"prompt", "approver", "subject"}): a consumed allow of a code
    prompt, or the rule's own approval when its subject is identical. It counts only where the code's author has no
    authority over the runner, and the stored check then carries it. Without a valid one there, the save raises
    NeedsCodeApproval (with the subject) when the runner can run code and the caller wrote that code."""
    if not isinstance(args, dict):
        raise Refused("save_trigger needs an object of fields.")
    extra = sorted(set(args) - SAVE_KEYS)
    if extra:
        raise Refused(f"save_trigger has no field {extra[0]}.")
    editing = args.get("trigger_id") is not None
    fields = {k: args[k] for k in RULE_FIELDS if k in args}
    if editing:
        tid = trigger_ref(args["trigger_id"])
        if rule is None or rule.get("id") != tid:
            raise Refused(f"There is no trigger {tid}.")
        base = revision_of(args)
        may_control(org, caller, rule)
        if not fields and "check" not in args:
            raise Refused("Give at least one of agent, schedule, task, late_minutes, max_tasks_per_day and check.")
        target = fields.get("agent", rule["agent"])
    else:
        if "base_revision" in args:
            raise Refused("base_revision belongs to a change of an existing trigger, with trigger_id.")
        missing = [k for k in ("schedule", "task") if k not in args]
        if missing:
            raise Refused(f"A new trigger needs {' and '.join(missing)}.")
        tid, base = None, None
        if fields.get("agent") is None:
            fields["agent"] = caller
        target = fields["agent"]
    if not isinstance(target, str) or not target:
        raise Refused("agent must be an agent name.")
    check_set(org, caller, target)
    if org.agents[target].get("active", True) is False:
        raise Refused(f"{target} is in setup. The owner activates it in Fleet.")
    route, why = wake_route(org, target)
    if route is None:
        raise Refused(why)
    task = fields.get("task", rule.get("task") if editing else None)
    if route["kind"] == "message" and isinstance(task, dict) and task.get("needs", "none") != "none":
        raise Refused("A wake-up of the manager is an inbox message, so task.needs must be none.")
    plan = {"op": "edit" if editing else "create", "trigger_id": tid, "base_revision": base,
            "fields": fields, "route": route, "source": None, "by": caller}
    old = rule.get("check") if editing else None
    if "check" in args or not editing:
        if args.get("check") is None:
            plan["check"] = None
        else:
            plan["check"], plan["source"] = saved_check(args["check"], caller, old)
    check = plan["check"] if "check" in plan else old
    moved = editing and target != rule["agent"]
    if check and check.get("code") == "saved" and ("check" in plan or moved):
        author, runner = check["code_by"], runner_of(target, check)
        subject = code_subject(target, check)
        allowed, why = code_grant(org, author, runner, approval, subject)
        controls = level(org, author, runner) >= SELF
        if not allowed:
            if author == caller and not controls and runner_problem(org, runner, author) is None:
                kept = f" Trigger {tid} stays unchanged until a save goes through." if editing else ""
                raise NeedsCodeApproval(
                    f"{author} has no authority over {runner}. The requester of your task can "
                    f"approve this exact code revision for {runner}: save it from a task session, and save it again "
                    f"after the decision.{kept}", subject)
            raise Refused(why)
        stored = {k: v for k, v in check.items() if k != "approval"}      # only the hub gives an approval
        if not controls:
            stored["approval"] = approval
        if "check" in plan or stored != check:
            plan["check"] = stored
    if not editing or target != rule["agent"]:
        have = sum(1 for r in rules if r.get("agent") == target and (r.get("created_by") or "owner") != "owner"
                   and r.get("state") != "deleted" and r.get("id") != tid)
        if have >= RULES_MAX:
            raise Refused(f"{target} has {have} wake-up rules that agents set; the limit is {RULES_MAX}. Delete one "
                          f"that it no longer needs first.")
    return plan


def plan_change(org, caller, args, rule):
    """The plan of one change_trigger call, or Refused: {"op": the action, "trigger_id", "base_revision", "by"}. The
    hub checks the trigger's state (TRIGGER_ACTIONS) and base_revision, and applies it as for the owner, with the
    caller's name as paused_by for a pause. check_now never starts a task."""
    if not isinstance(args, dict):
        raise Refused("change_trigger needs an object of fields.")
    extra = sorted(set(args) - CHANGE_KEYS)
    if extra:
        raise Refused(f"change_trigger has no field {extra[0]}.")
    tid = trigger_ref(args.get("trigger_id"))
    if rule is None or rule.get("id") != tid:
        raise Refused(f"There is no trigger {tid}.")
    action = args.get("action")
    if action not in ACTIONS:
        raise Refused("action must be pause, resume, delete or check_now.")
    base = revision_of(args)
    may_control(org, caller, rule, action)
    if action == "check_now" and not rule.get("check"):
        raise Refused(f"Trigger {tid} has no check. Add one with save_trigger, or wait for its time.")
    if action in ("resume", "check_now"):
        valid, why = still_valid(org, rule)
        if not valid:
            raise Refused(why)
    return {"op": action, "trigger_id": tid, "base_revision": base, "by": caller}


def command_identity(caller, tool, args):
    """(request_id, digest): the persisted identity of one write by an agent tool. The hub keeps (caller, request_id,
    tool, digest, receipt) in the same transaction as the write. An exact repeat (the same caller, request_id, tool
    and digest) gets the stored receipt again and changes nothing, also after the rule is done or deleted and after a
    restart. The same request_id with another tool or other fields is refused. A refused command stores nothing, so
    its request_id can be used again. The digest covers every field except request_id and task_id."""
    rid = args.get("request_id") if isinstance(args, dict) else None
    if not isinstance(rid, str) or not REQUEST.fullmatch(rid):
        raise Refused("request_id is required: a new text of 8 to 64 letters, digits, '.', '_', ':' or '-' for each "
                      "change, for example a UUID. Send the same request_id again only to repeat the same change.")
    body = {k: v for k, v in args.items() if k not in ("request_id", "task_id")}
    digest = hashlib.sha256(json.dumps([tool, body], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                       default=str).encode("utf-8")).hexdigest()
    return rid, digest


def same_command(stored, tool, digest):
    """None for an exact repeat of a stored command ({"tool", "digest", "receipt"}), else Refused."""
    if stored["tool"] != tool or stored["digest"] != digest:
        raise Refused("This request_id was already used for another change. Use a new request_id.")


def receipt(op, trigger_id, revision, run_id=None):
    """What the hub stores for one accepted write, and gives again for an exact repeat."""
    return {"op": op, "trigger_id": trigger_id, "revision": revision, "run_id": run_id}


def receipt_text(r, replayed=False):
    verb = {"create": "Created", "edit": "Changed", "pause": "Paused", "resume": "Resumed", "delete": "Deleted",
            "check_now": "Sent the check of"}[r["op"]]
    text = f"{verb} trigger {r['trigger_id']} (revision {r['revision']})."
    if r.get("run_id"):
        text += f" Run {r['run_id']}: read its result with see_triggers in a minute."
    if replayed:
        text += " This repeats an earlier request with the same request_id, so nothing new was done."
    return text


def history_detail(action, old, new):
    """The detail of one trigger_history row. An edit gives each changed field before and after; a saved check
    appears with its SHA-256, author and runner, never with its text. Another action gives the state before and
    after."""
    keys = RULE_FIELDS + ("check", "created_by")
    if action == "create":
        return {"created": {k: new.get(k) for k in keys}}
    if action == "edit":
        return {"changed": {k: {"before": old.get(k), "after": new.get(k)} for k in keys if old.get(k) != new.get(k)}}
    return {"state": {"before": old.get("state"), "after": new.get("state")}}


# ---------------------------------------------------------------- what a wake-up starts

def wake_armed(last, quiet_after, repeat_minutes, t0, when):
    """(armed, reason, final) of a check rule, as hub.trigger_armed, for any route. last is what the newest started
    run started: None, or {"ref": "T-41" or "M-812", "open": bool, "ended": a final task state, or "read" for a
    manager message, "ended_at": epoch or None}. For a parent task route, last is open while either task is open.
    quiet_after: a quiet run came after that run. final: only a quiet check or Run task now arms the rule again."""
    if last is None or quiet_after:
        return True, None, False
    if repeat_minutes is None:
        return False, "Waiting for a quiet check.", True
    if last["open"]:
        return False, f"{last['ref']} is still {'unread' if last['ref'].startswith('M-') else 'open'}.", False
    if last["ended"] not in ("completed", "read"):
        return False, (f"{last['ref']} ended {last['ended']}. Only a quiet check or Run task now starts the next "
                       f"one."), True
    again = (last["ended_at"] or 0) + repeat_minutes * 60
    if t0 < again:
        return False, f"Again at {when(again)}.", False
    return True, None, False


def manager_decision(facts, when):
    """(state, reason) of one run whose route is a message to the fleet manager. "started" means that the hub writes
    that message in the run's transaction. The rules come in the order of the hub's trigger_decision (prompt runs)
    and check_decision (check runs); the first that applies wins. The hub applies the late rule ("missed") and
    still_valid before, and reads these facts:

    check: a check run. matches: the check's value matches the condition. check_now: a Check now run.
    repeat_minutes, last, quiet_after: as for wake_armed. started_24h and max_per_day: this trigger's wake-ups in
    the last 24 hours and its limit. fleet_24h and fleet_cap: the same for all triggers. limit_until: the end of the
    manager's usage limit, or None. t0: now.

    A prompt run gives "started" or "not_started" (never tried again). A check run gives "quiet", "not_started"
    (final), "waiting" (tried again on fresh data) or "started". Only "started" writes anything that a model reads."""
    check, last = facts["check"], facts["last"]
    if check:
        if not facts["matches"]:
            return "quiet", None
        if facts.get("check_now"):
            return "not_started", "Check now never starts a wake-up."
        armed, why, final = wake_armed(last, facts["quiet_after"], facts["repeat_minutes"], facts["t0"], when)
        if not armed:
            return ("not_started" if final else "waiting"), why
    wait = "waiting" if check else "not_started"
    if last and last["open"]:
        return wait, f"{last['ref']} is still unread."
    if facts["started_24h"] >= facts["max_per_day"]:
        return wait, (f"This trigger started {facts['started_24h']} wake-ups in the last 24 hours (limit "
                      f"{facts['max_per_day']}).")
    if facts["fleet_24h"] >= facts["fleet_cap"]:
        return wait, (f"Triggers reached the fleet limit: {facts['fleet_24h']} in the last 24 hours (limit "
                      f"{facts['fleet_cap']}).")
    if check and facts.get("limit_until"):
        return "waiting", f"The manager is at its usage limit until {when(facts['limit_until'])}."
    return "started", None


def manager_message(trigger_id, fields, line):
    """The inbox message of a fleet-manager wake-up. It starts with [hub], as every hub message does, and ends with
    the provenance line of hub.trigger_provenance (which, for a check, says what the check found)."""
    parts = [f"[hub] Wake-up from trigger {trigger_id}: {fields['title']}"]
    if fields.get("details"):
        parts.append(fields["details"])
    parts += [f"Done when: {fields['done_when']}", line]
    return "\n".join(parts)


def parent_fields(trigger_id, fields, route, line):
    """The task fields of the parent task of a route with a parent: the requester's own task, in whose session the
    target's result arrives. The hub inserts it first, then the target's task with parent = its id, in one
    transaction with the run."""
    owner, requester = route["owner"], route["requester"]
    return {"owner": requester, "title": f"Wake-up for {owner} from trigger {trigger_id}: {fields['title']}"[:200],
            "details": (f"Trigger {trigger_id} started a subtask of this task for {owner}: {fields['title']}. Its "
                        f"result, its questions and its permission prompts arrive in this session. Check the result "
                        f"with check_result and answer with answer; then deliver this task.\n\n{line}"),
            "done_when": f"The subtask for {owner} has ended and you have checked its result.",
            "needs": "none", "goal_id": fields.get("goal_id")}


# ---------------------------------------------------------------- tool texts

def schedule_text(s):
    if s["kind"] == "every":
        return f"every {s['minutes']} minutes"
    if s["kind"] == "once":
        return f"once, at {s['at'].replace('T', ' ')} {s['timezone']}"
    if s["kind"] == "daily":
        days = "every day" if not s.get("days") else ", ".join(s["days"])
        return f"{days} at {s['time']} {s['timezone']}"
    return f"day {s['day']} of each month at {s['time']} {s['timezone']} (a month without that day is skipped)"


def approval_ref(approval):
    """P-<n>: the code prompt of a stored approval."""
    prompt = approval.get("prompt")
    return prompt if isinstance(prompt, str) else f"P-{prompt}"


def check_text(c, target):
    if not c:
        return "none: the task starts at each time"
    a = c.get("approval")
    approved = f", approved by {a.get('approver')} in {approval_ref(a)}" if isinstance(a, dict) else ""
    code = (f"saved code {c['sha256'][:12]} ({c['bytes']} bytes, by {c['code_by']}, runs on "
            f"{c.get('run_on') or target}{approved})" if c.get("code") == "saved"
            else f"{c['path']} at {c['commit'][:7]}, pinned by the owner")
    cond = c["condition"]
    repeat = ("once, until a check is quiet again" if c["repeat_minutes"] is None else
              f"again {c['repeat_minutes']} minutes after the last wake-up completed, while the check still matches")
    return f"{code}; it starts the wake-up when the value is {OPS[cond['op']]} {cond['value']:g}; {repeat}"


def run_text(r, when):
    out = f"run {r['id']} for {when(r['due'])}: {r['state']}"
    if r.get("value") is not None:
        out += f", value {r['value']:g}" + (f" ({r['summary']!r})" if r.get("summary") else "")
    if r.get("error"):
        out += f", error {r['error']}" + (f" ({r['detail']})" if r.get("detail") else "")
    if r.get("task"):
        out += f", T-{r['task']}"
    if r.get("event_id"):
        out += f", M-{r['event_id']}"
    return out + (f". {r['reason']}" if r.get("reason") else ".")


def describe(rule, when):
    """One rule as lines for an agent. when(ts) gives a time in the rule's time zone (hub.trigger_when)."""
    state = rule["state"]
    if state == "paused":
        state = f"paused by {rule.get('paused_by') or 'the owner'}" + (
            f": {rule['paused_reason']}" if rule.get("paused_reason") else "")
    task = rule["task"]
    lines = [f"Trigger {rule['id']} (revision {rule['revision']}), set by "
             f"{rule.get('created_by') or 'owner'}, wakes {rule['agent']}: {state}.",
             f"  Schedule: {schedule_text(rule['schedule'])}." +
             (f" Next: {when(rule['next_due'])}." if rule.get("next_due") else ""),
             f"  Task: {task['title']} (needs {task['needs']}). Done when: {task['done_when']}",
             f"  Check: {check_text(rule.get('check'), rule['agent'])}."]
    armed = rule.get("armed")
    if armed and not armed.get("armed"):
        lines.append(f"  Armed: no. {armed.get('reason') or ''}".rstrip())
    if rule.get("failures"):
        lines.append(f"  Check errors in a row: {rule['failures']} (the hub pauses the rule at 5).")
    if rule.get("last_run"):
        lines.append(f"  Last {run_text(rule['last_run'], when)}")
    lines.append(f"  Wake-ups in the last 24 hours: {rule.get('tasks_24h', 0)} of {rule['max_tasks_per_day']}.")
    return "\n".join(lines)


def describe_detail(rule, runs, history, source, when):
    """One rule with its details text, its newest runs, its change history and its saved code, for see_triggers
    with trigger_id. history rows: {"revision", "ts", "by", "action", "detail"}. The code is untrusted text."""
    out = [describe(rule, when)]
    if rule["task"].get("details"):
        out.append("  Details: " + rule["task"]["details"])
    out.append("Runs, newest first:" if runs else "No runs yet.")
    out += [f"  {run_text(r, when)}" for r in runs]
    if history:
        out.append("Changes, newest first:")
        for h in history:
            what = h.get("detail") or {}
            fields = ", ".join(sorted(what.get("changed", {}))) or (
                "{} to {}".format(what["state"]["before"], what["state"]["after"]) if "state" in what else "")
            out.append(f"  revision {h['revision']} at {when(h['ts'])} by {h['by']}: {h['action']}" +
                       (f" ({fields})" if fields else ""))
    if source is not None:
        out += [f"Saved check code, SHA-256 {rule['check']['sha256']}:", "```python", source.rstrip("\n"), "```"]
    return "\n".join(out)


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


TASK_ID = {"type": "string", "description": "In a task session: your task, for example T-7. You can leave it out."}
TRIGGER_ID = {"type": "integer", "description": "The trigger number, for example 7."}
BASE = {"type": "integer", "description": "The revision of the trigger that see_triggers shows. A trigger that "
                                          "changed meanwhile is refused: read it again."}
REQUEST_ID = {"type": "string", "description": "A new unique text for each change (8 to 64 letters, digits, '.', '_', "
                                               "':' or '-'), for example a UUID. Repeat it only to repeat the same "
                                               "change: you get the first answer again."}

TOOL_SPECS = (
    ("see_triggers",
     "See the wake-up rules (triggers) that you may see: those that wake you, those that you set, and all of them "
     "when you are the manager. With trigger_id: one rule with its newest "
     "runs, its change history and its saved check code.",
     obj({"trigger_id": TRIGGER_ID, "task_id": TASK_ID})),
    ("save_trigger",
     "Create a wake-up rule, or change one (with trigger_id and base_revision; give only the fields that change). "
     "A rule wakes its agent on a schedule. Without a check, it starts its task at each time, which uses model "
     "tokens. With a check, it first runs saved check code outside every AI session, and it starts the task only "
     "when the check's value matches the condition: a check that does not match starts no model. You set rules for "
     "yourself; the manager for any agent, itself included (a wake-up of the manager is an inbox message). The "
     "task's requester is the agent's own requester; its result arrives as for every task.\n"
     "schedule: {\"kind\":\"once\",\"at\":\"2026-10-14T09:00\",\"timezone\":\"Europe/Madrid\"}, "
     "{\"kind\":\"every\",\"minutes\":15} (5 to 10080), "
     "{\"kind\":\"daily\",\"time\":\"09:00\",\"days\":[\"mon\",\"thu\"] or null,\"timezone\":\"Europe/Madrid\"}, or "
     "{\"kind\":\"monthly\",\"day\":1,\"time\":\"09:00\",\"timezone\":\"Europe/Madrid\"}.\n"
     "task: {\"title\", \"details\", \"done_when\", \"needs\": \"none\", \"browser\" or \"screen\", \"goal_id\": a "
     "goal number or null}.\n"
     "check: {\"code\": a Python 3 program, \"condition\": {\"op\": \">\", \">=\", \"<\", \"<=\", \"==\" or \"!=\", "
     "\"value\": a number}, \"repeat_minutes\": null (once, until a check is quiet again) or 0 to 10080 (again that "
     "many minutes after the last task completed, while the check still matches), \"run_on\": an optional agent "
     "whose machine runs the check, \"args\": an optional JSON object of at most 4 KiB, \"timeout_seconds\": 1 to 60, "
     "default 30}. The check runs only on an agent whose runtime already runs any command without asking, and that "
     "you already control; no Claude Code agent can run checks yet. So a Claude Code agent names "
     "such an agent in run_on; a refusal lists the ones that you control. If you control none, save the check from a "
     "task session with run_on naming a runner that the requester of your task controls: the hub asks that requester "
     "to approve this exact code, its args, its runner and its time limit, and you save the same check again after "
     "the decision. Until a save goes through, nothing is saved, and an existing rule stays as it is. Each new code "
     "revision needs a new decision; you get no code access of your own. The program gets one argument, the JSON "
     "{\"trigger\", \"run\", "
     "\"args\"}, and must print exactly one JSON object and exit with 0: {\"value\": a number, \"summary\": at most "
     "200 characters} or {\"error\": a code of a-z and _, \"detail\": text, \"retry_after\": 60 to 86400 seconds}. "
     "It runs with python3 -I as that machine's user, in an empty folder, with no Fleet token and no login. Never put "
     "a login, a token or another secret in the code or the args. In a change, a check without code keeps the saved "
     "code, and \"check\": null removes the check.",
     obj({"request_id": REQUEST_ID, "trigger_id": TRIGGER_ID, "base_revision": BASE,
          "agent": {"type": "string", "description": "The agent that the rule wakes. Default: you."},
          "schedule": {"type": "object", "description": "When the rule runs. See the forms above."},
          "task": {"type": "object", "description": "What the agent does when the rule wakes it."},
          "late_minutes": {"type": "integer", "description": "0 to 1440, default 60: a later run starts nothing."},
          "max_tasks_per_day": {"type": "integer", "description": "0 to 48, default 4: wake-ups in any 24 hours."},
          "check": {"description": "The check object above, or null for no check."},
          "task_id": TASK_ID}, ["request_id"])),
    ("change_trigger",
     "Pause, resume or delete a wake-up rule that you may change, or run its check once now (check_now never starts "
     "a wake-up; read its result later with see_triggers). Only the owner resumes a rule that the owner paused.",
     obj({"request_id": REQUEST_ID, "trigger_id": TRIGGER_ID, "base_revision": BASE,
          "action": {"type": "string", "enum": list(ACTIONS)}, "task_id": TASK_ID},
         ["request_id", "trigger_id", "base_revision", "action"])),
)
