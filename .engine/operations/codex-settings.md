---
title: Codex settings for Engine operations — audited ownership, defaults, and limits
---

## Purpose

This is the Engine's canonical Codex-settings policy: which Codex controls materially affect Engine
operations, what the Engine may configure in a repository, what stays the operator's personal choice, and
which apparent controls do not provide the isolation they seem to. Other Engine pages point here instead of
repeating permission or scheduling advice.

The audit was performed **2026-08-12** against Codex CLI **0.147.0-alpha.6.5**, the Codex desktop settings
surface, and the current official Codex documentation. Re-audit this page when those platform facts change.
The evidence of record is the official documentation for [configuration precedence](https://learn.chatgpt.com/docs/config-file/config-basic#configuration-precedence),
[permissions](https://learn.chatgpt.com/docs/permissions), [sandboxing and approvals](https://learn.chatgpt.com/docs/agent-approvals-security),
[custom agents](https://learn.chatgpt.com/docs/multi-agent), [scheduled tasks](https://learn.chatgpt.com/docs/automations),
and [desktop settings](https://learn.chatgpt.com/docs/reference/settings).

**Acceptance record, 2026-08-12.** The three open policy questions are resolved from current official
precedence, custom-agent, and Automation behavior: the live task override outranks project configuration; a
parent override can displace a spawned agent's requested default; and schedules share one default with no
per-Automation profile. This is documented-platform acceptance, not a claim that the post-merge end-to-end
arms already ran on every host surface. `codex-validation.md` keeps those live arms as a hard pre-release gate;
a mismatch reopens the policy before release.

For every row below, **owner** says where the choice legitimately belongs; **verdict** says what to use;
**why / fallback** says both the operational value and what still works if the operator declines it. A
project file is never described as authoritative where a higher-precedence live control can replace it.

## Steps

### Apply the operating baseline

- **Ordinary Engine work: Workspace Write + Ask for Approval.** Make this the desktop default if this
  machine primarily uses Codex for Engine-managed projects. It lets the Engine use its workspace and OS
  temporary directory while keeping home-directory writes, shell network access, and broader machine access
  behind review. **Before changing the shared default, inventory every existing scheduled task; disable any
  forgotten or untrusted one and explicitly accept Workspace Write for each task left enabled.** Read Only
  remains a useful task-level choice for inspection. Full Access is not an Engine baseline.
- **Live task selection wins.** The permission selected beneath the composer is a live runtime override and
  outranks repository configuration. Changing an interactive task to Workspace Write changes that task; it
  does not prove that another task or a schedule uses the same mode.
- **Scheduled tasks share one default.** Codex currently provides no per-schedule sandbox profile. Scheduled
  tasks use the default sandbox and use `approval_policy = "never"` when organization policy permits it.
  A write-capable Engine routine would therefore make *every* scheduled task that shares that default
  write-capable; this is why Codex build Automations are retired. Do not call two differently instructed
  schedules isolated when the platform gives them the same effective boundary.
- **Reviewer files request; they do not confine.** Engine reviewer TOMLs request `read-only`, which is the
  right standalone default. Codex reapplies the parent task's live runtime override to a spawned custom agent,
  so a reviewer launched from a Workspace Write task may receive Workspace Write. Its no-write instruction and
  the operator's merge remain the bounds; the child TOML is not a mechanical separation wall.
- **The Engine does not write sandbox or approval defaults into `.codex/config.toml`.** That file is shared
  operator/project configuration, the live selector can supersede it, and legacy `sandbox_mode` settings do
  not compose with beta permission profiles. The Engine manages only its own fenced MCP registrations there.
- **Codex Automations are not an Engine write path.** The shared scheduled sandbox, never-ask execution,
  repository-wide push credentials, push-triggered workflows, separate plugin/connection authority, and GitHub's
  direct/deferred merge surfaces cannot currently preserve the Engine's promise that only the operator merges.
  **Before merging or releasing this policy, disable every existing `$engine-routine` Automation and confirm
  none remains Active.** The generated skill refuses with migration guidance, but a shell process cannot
  mechanically prove which scheduler launched it; disabling the external task is the real retirement boundary.
  Keep Codex Engine work interactive; use the supported Claude Desktop Routine when unattended writes are
  required. This removes the unsafe host ceremony rather than
  asking every operator to reproduce it in UI.
- **Credential masking is a partial host-hardening layer.** Keep reusable credentials out of interactive
  spawned shells. Prefer the OS credential store for Codex
  login and enable Codex's automatic secret-name exclusions:

  ```toml
  cli_auth_credentials_store = "keyring"

  [shell_environment_policy]
  ignore_default_excludes = false
  ```

  This filters likely secret-bearing environment variables by name; it cannot redact a credential deliberately
  put in a prompt, file, command argument, or tool output. If authentication requires an agent-visible reusable
  secret, keep the task local and push interactively from a separately authenticated shell.

### Audit the visible desktop settings

| Settings area | Owner | Verdict for Engine operations | Why, enforcement, and fallback |
|---|---|---|---|
| General — sandbox / permissions | Operator; live task control | **Workspace Write + Ask for Approval** as the default; Read Only for inspection; never routine Full Access | This is the one manual choice with outsized value: without Workspace Write, Build cannot write its session marker or files; without approvals, a request beyond the workspace has no human review. If declined, the Engine remains useful for reading, diagnosis, and planning but must not claim it built. |
| General — web search | Operator default, task judgment | Keep **Cached** by default; use Live only when the answer depends on facts that may have changed since the cache | Web search is separate from shell network. Cached lowers exposure to arbitrary live pages. A current release, security advisory, price, law, schedule, or version requires Live; otherwise cached or no search is sufficient. |
| General — output detail | Operator preference | No Engine default | It changes presentation, not Engine correctness. The codes of conduct and task prompt carry the useful response contract. |
| General — reasoning summary | Operator preference | Auto or the operator's preference; no Engine default | It changes the visible explanation, not the review or merge gates. |
| General — prevent sleep / follow-up behavior | Operator; conditional | Enable Prevent Sleep only on a machine intentionally running local schedules; choose follow-up behavior personally | Prevent Sleep has outsized value only for unattended local work; the fallback is to keep the machine awake or accept that the schedule will not fire. Follow-up behavior is conversational preference. |
| General — require Cmd+Enter for multiline prompts | Operator | No Engine default | This prevents accidental submission for operators who value it but does not change Engine authority or correctness. If disabled, Enter keeps its normal submit behavior. |
| Import | Operator | No Engine action | Moving personal ChatGPT data does not improve a repository's Engine. |
| Profile | Operator | No Engine action | Identity and activity insights are account concerns, not project policy. |
| Notifications | Operator; conditional | Enable completion / permission notifications when unattended or long-running work makes them useful; no Engine default | Notifications improve awareness but do not enforce a gate. If declined, progress remains in the task or schedule history and pull request. |
| Appearance | Operator | No Engine action | Theme and fonts do not affect Engine operation. |
| Voice | Operator | No Engine action | Input/output modality does not change project permissions or evidence. |
| Configuration | Shared: operator plus Engine-owned fences | Engine manages only fenced MCP registrations; preserve all operator keys | The module wiring seam applies and reverses Engine blocks without taking ownership of the whole TOML file. Sandbox, model, personality, and approval choices remain outside those fences. |
| Personalization / Memories | Operator | No Engine defaults; do not use ChatGPT memory as project truth | Personality and custom instructions are personal. Project state and decisions live in the Engine; ChatGPT memory is not evidence about the project. |
| Suggested prompts | Operator | No Engine action | Suggestions are navigation conveniences, not project plans or remembered decisions. The Engine's settled plan and state remain authoritative if suggestions are disabled or irrelevant. |
| Pets | Operator | No Engine action | Cosmetic only. |
| Keyboard shortcuts | Operator | No Engine action | Ergonomic only; no cross-repository value justifies intervention. |
| Usage & billing | Operator / organization | Observe limits when work stops; no Engine mutation | Entitlements and limits can prevent a task from running but are not project configuration. |
| Account | Operator / organization | No Engine action | Authentication and subscription administration remain outside the repository. |
| Appshots | Operator | No Engine default | Useful only when the operator chooses to share an app-state capture; no standing Engine dependency. |
| Plugins | Operator / organization | Install only for a concrete task with explicit consent | Plugins expand data and action reach. The Engine offers an applicable add-on or plugin; it never installs one speculatively. |
| Browser | Operator | Conditional, per task | Use when signed-in browser state is actually needed. Website allow/block choices are personal and higher-risk than ordinary web search; the fallback is an API, repository data, or no browser action. |
| Computer use | Operator + OS | Off unless a concrete task requires visible UI control | Screen Recording / Accessibility are broad host grants. Their value is outsized only for a task that cannot be completed through files, APIs, or a purpose-built connector. |
| Hooks | Engine registration; operator trust | **Approve once and re-approve after the Engine changes its hooks** | Codex will not run new or changed project hooks without trust. This manual intervention has outsized value because grounding, the Explore write-gate, and memory capture otherwise turn off. If declined, the assistant must disclose that automation is off, ground manually, and stay read-only until Build is explicit. |
| Connections | Operator | Configure only for a named remote target a task requires | A connection expands the machine/repository boundary. No generic Engine connection is justified. |
| Git | Operator identity; Engine workflow | Keep credentials in OS helpers; use isolated worktrees and pull requests interactively | The Engine never changes the operator checkout's history and never merges. If a safe helper credential is unavailable, work locally and push from a separately authenticated shell. Codex Automations do not receive repository-write credentials. |
| Environments | Operator / project tooling | Use only when the project's own runtime needs one | The Engine carries its own uv-managed tool runtime; it does not create a second host environment merely because the setting exists. |
| Worktrees | Engine workflow with platform support | Keep enabled; use dedicated worktrees for builds and schedules | Isolation protects the operator's checkout and is central to the PR-only workflow. If unavailable, scheduled Routine refuses to write; an interactive build must use another isolated copy. |
| Archived chats | Operator | No Engine action | Archival is conversation housekeeping, not project memory or completion evidence. |

### Audit the operational configuration families

| Family | Engine recommendation and authority |
|---|---|
| `sandbox_mode`, `approval_policy`, permission profiles | Use the operating baseline above. Do not mix legacy sandbox settings with permission profiles. The Engine documents and diagnoses; it does not force a project default that a live task can replace. |
| Shell network | Off for ordinary interactive work; enable only for the current task and constrain sandboxed command traffic with a named permission profile or `[features.network_proxy]` domain rules. Do not mix permission profiles with legacy sandbox settings. Codex Automations are not an Engine write path and receive no standing GitHub network/credential posture. |
| Models and reasoning effort | Keep model selection current at the platform/account layer. Engine personas specify a reasoning tier, never a model id that will rot. |
| MCP servers | Engine servers are registered in Engine-owned fenced TOML blocks; other servers are operator-owned. Adding one is an explicit trust decision. |
| Skills and agents | Engine skills and Codex agent renders are committed, generated adapter surfaces held in sync by checks. Their behavioral instructions travel; a parent task's live permissions still govern effective capability. |
| Rules / command allowlists | No blanket Engine allowlist. A rule that bypasses an approval is a host decision and must be narrower than the concrete command/use case. Rules never replace the protected merge. |
| Automations | Use for personal read-only reminders or monitoring only when the shared default is appropriately narrow. Disable old `$engine-routine` and scheduled self-review Automations. Engine unattended writes use the Claude Desktop routine; Codex self-review runs interactively in Read Only. |
| Hooks | Repository-owned registration plus operator trust. A changed hook is deliberately off until re-trusted; the Engine must say so rather than pretending its gates ran. |
| Telemetry, notifications, output, personality | Personal/organizational choices. They may improve observability or comfort but do not change Engine evidence or authority. |

### Run the live acceptance bar

Repository checks can prove the files, defaults, and renders are coherent. They cannot prove what the desktop
applied. After a Codex adapter or settings-policy change, follow `codex-validation.md`: verify the interactive
Read Only and Workspace Write task paths separately; verify a reviewer spawned under each parent and record
that the parent live override governs; verify a scheduled task uses the one shared default; and verify that no
screen offers a per-Automation sandbox profile. Run
`uv run --directory .engine --frozen -- python tools/demo_uv_workspace_cache.py` to prove the real manual
grounding path uses `.engine/.uv`, succeeds with an unusable home directory, and leaves tracked worktree state
unchanged. A changed platform result reopens this audit rather than being papered over with conditional prose.
Before merging this change, record every live arm as passed on the supported Codex host and confirm no retired
Codex Automation remains Active; otherwise stop the merge and release. Before cutting the release, retain that
record;
the documentation-level acceptance record above is sufficient to design the policy, not to waive live rollout.

## Done when

Every visible settings category and operational configuration family above has a recorded owner, verdict,
reason, enforcement limit, fallback, and evidence baseline; the live checks have either confirmed the current
platform behavior or reopened this audit; and every other Engine page points here instead of inventing a
second permission or scheduling policy.

## Notes

Three platform dependencies remain observational rather than Engine-controlled. First, the host sandbox is a
complementary boundary around the Engine's deliberately fallible hooks; Workspace Write makes the current task's
workspace roots writable, not every checkout on the machine. An Engine mechanic working on an owned product in
a separate checkout must either run a task rooted at that product worktree or request the narrow outside-workspace
approval — never switch the whole session to Full Access merely to avoid that prompt. Second, project
configuration loading across the desktop app, CLI, and IDE remains a Codex dependency, and every surface still
applies the documented precedence chain plus any live runtime override; `codex-validation.md` is the acceptance
bar, not an assumption of parity. Third, a scheduled "dedicated background worktree" must be a git-linked
worktree that the Engine's isolation proof recognizes. A platform-created directory with some other isolation
shape is a defect to surface and adapt to, not proof that the gate passed.

The uv cache is disposable Engine runtime state. Every real upgrade re-sync automatically runs
`uv cache prune` after `uv sync --frozen`, removing unused entries without touching the locked environment.
An operator may run `uv --directory .engine cache prune` at any time for the same bounded cleanup; deleting
`.engine/.uv/` entirely is also safe while no Engine command is running because uv recreates it from the lock.
