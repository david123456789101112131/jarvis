# Testing JARVIS

Every stage of JARVIS was checked before the next one started; this is how to run those checks
again, what each layer covers, and where it can run.

Nothing here touches your real JARVIS data: every test gets a fresh temporary `JARVIS_HOME` and
`JARVIS_KEYRING=0` (so API keys stored in Windows Credential Manager are never read or changed), the
end-to-end tests start their own JARVIS processes on free ports and stop them through the API, and
`jarvis check --full` runs its JARVIS the same way, in a temporary folder that it deletes at the end.

## The layers

| Layer | What it proves | Where it runs | Command |
|---|---|---|---|
| Unit | each part on its own: intents, agents, orchestrator, permissions, tools, memory, voice pipeline, host adapters | any OS | `python -m pytest tests/unit` |
| Integration | the local server (auth, REST, WebSocket) and the CLI against a real core | any OS | `python -m pytest tests/integration` |
| End to end: process | a real `python -m jarvis --no-window` process driven over HTTP and WebSocket, the way the window drives it | any OS | `python -m pytest tests/e2e` |
| End to end: window | the built interface in a real browser engine against a real JARVIS: typing, clicking, approvals, the kill switch | any OS with Chromium, Edge or Chrome | `python -m pytest tests/e2e/test_ui_browser.py` |
| Real Windows | Start-menu apps, clipboard, volume, known folders, process flags, the one-call process list, the system monitor on a real desktop | Windows | `python -m pytest -m windows` |
| Real voice models | openWakeWord hears "Hey Jarvis", faster-whisper transcribes speech | anywhere the models are downloaded | `python -m pytest -m voice_models` |
| Interface | the UI's state, Markdown and path rendering, the approval dialog, translations | Node.js 20+ | `cd ui; npm test` |
| Static | lint, types (checked as Windows), the interface's types | any OS | `ruff check src tests scripts`; `python -m mypy`; `cd ui; npm run typecheck` |
| Packaged app | the PyInstaller build starts without a console and passes `jarvis-cli check --full` as built; the installer compiles | Windows (the PyInstaller part also builds on Linux) | `scripts\build.ps1` |
| On the PC | is this computer ready, and does JARVIS really work here? | the user's PC | `jarvis check` (see below) |

`python -m pytest` runs all the Python layers. A test that needs something this computer does not
have (Windows, a browser, the models, a microphone) is skipped with the reason, never passed
silently: `python -m pytest -rs` lists the reasons.

### Setting up

`setup.bat -Dev` does all of this on Windows (except the browser download). By hand:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -e ".[all,dev]"
python -m playwright install chromium      # for the window tests (or use Edge, see below)
cd ui; npm ci; npm run build; cd ..          # the interface the window tests load
```

### Environment variables

| Variable | Effect |
|---|---|
| `JARVIS_TEST_BROWSER` | path of a Chromium-based browser for the window tests. Without it they use Playwright's Chromium, then Microsoft Edge, then Google Chrome, and skip when none starts. |
| `JARVIS_TEST_MODELS_DIR` | a folder with `openwakeword/` and `whisper/` models for the `voice_models` tests. Without it they look in JARVIS's own model cache (filled the first time voice runs, or by `jarvis voice-test`). |
| `JARVIS_TEST_MEDIA_PYTHON` | a Python with the media runtime (torch, diffusers, transformers, imageio-ffmpeg; `<media>\venv\Scripts\python.exe` after `jarvis media install`) for `integration/test_local_media_real.py`: the real worker, diffusers' own Z-Image and Wan pipelines on tiny random models built on the spot, on the processor. Skipped without it. |

### Markers

| Marker | Meaning |
|---|---|
| `e2e` | starts a real JARVIS process (and, for the window tests, a browser). `-m "not e2e"` skips them for a quick run. |
| `windows` | needs a real Windows desktop session. |
| `voice_models` | needs the downloaded wake-word / speech-recognition models. |
| `network` | needs the internet. |
| `slow` | takes more than a few seconds. |

Coverage: `python -m pytest --cov=jarvis --cov-report=term-missing -m "not e2e"` (the end-to-end
tests run JARVIS in another process, which the coverage of this one does not see).

## `jarvis check` on the PC

The same checks a developer runs, packaged for the computer JARVIS is installed on. Each line ends
`OK`, `WARN`, `FAIL` or `SKIP` with a short explanation, and a `→` line says what to do about a
warning or failure. The exit code is 0 unless something failed; `--json` prints the report as JSON.

| Command | What it does | Time |
|---|---|---|
| `jarvis check` | Python and Windows version, JARVIS's folders and free space, the memory database and its Hebrew search, a local port, the built interface and the WebView2 runtime, Start-menu apps, volume and brightness, where API keys are kept (names only, never values), the AI provider (no request is sent), Git, Node.js and Python for projects, the internet, the voice packages, whether the computer-control model is installed (it is not started) | seconds |
| `jarvis check --full` | everything above, then a second JARVIS in a temporary folder runs real commands: creates a folder, checks the computer, builds a website with the agents and verifies it, refuses and then approves a permanent delete, engages and releases the kill switch, and remembers a fact across a restart | usually under a minute |
| `jarvis check --voice` | everything above, with the whole voice self-test instead of the package check: microphone, speakers (it says two sentences), text-to-speech in Hebrew and English, speech recognition, the wake word | about a minute (the first run downloads the models) |
| `jarvis voice-test` | only the voice self-test | about a minute |

## Spec §27: the mandatory tests

Each required test, the tests that cover it on its own, and the tests that cover it through a running
JARVIS. Paths are under `tests/`; `::` names a class or test.

| Required test | Covered by (unit and integration) | Through a running JARVIS |
|---|---|---|
| הפעלת Agent (start an agent) | `unit/test_agents.py::TestManager::test_stop_start_and_restart_an_agent`; `unit/test_core.py::TestTasks::test_agent_control`; `unit/test_orchestrator.py::TestControl::test_restarting_an_agent_restarts_its_step` | `e2e/test_ui_browser.py::test_a_task_runs_in_the_window_from_plan_to_result` (the card's Start button); `jarvis check --full` |
| עצירת Agent (stop an agent) | `unit/test_agents.py::TestManager::test_stopping_an_agent_ends_its_running_and_queued_work`, `::test_agent_kill_switch`; `unit/test_orchestrator.py::TestControl::test_a_stopped_agent_pauses_the_task`, `::test_an_optional_step_of_a_stopped_agent_is_skipped`; `unit/test_core.py::TestFastPath::test_a_stopped_agent_says_so` | `e2e/test_live_process.py::test_the_agents_build_and_verify_a_project_together`; `e2e/test_ui_browser.py::test_a_task_runs_in_the_window_from_plan_to_result` (the Stop button) |
| שליחת Task (send a task) | `unit/test_agents.py::TestManager::test_assignment_and_result_travel_on_the_bus`; `unit/test_core.py::TestTasks::test_give_an_agent_a_task`, `::test_build_a_store_website`; `integration/test_server.py::TestRest::test_command_runs_through_the_core` | `e2e/test_live_process.py::test_commands_act_on_the_computer_and_the_window_follows_live`; `e2e/test_ui_browser.py::test_the_window_shows_live_state_and_runs_commands` |
| חלוקת Task (split a task) | `unit/test_orchestrator.py::TestScheduling::test_dependencies_order_and_context`, `TestFlagship::test_build_a_coffee_shop_website_with_all_five_agents`, `TestPlanningAndAI` | `e2e/test_live_process.py::test_the_agents_build_and_verify_a_project_together`; `e2e/test_ui_browser.py::test_a_task_runs_in_the_window_from_plan_to_result` (the six steps of the plan) |
| עבודה במקביל (parallel work) | `unit/test_orchestrator.py::TestScheduling::test_independent_steps_run_in_parallel`, `::test_all_five_agents_work_at_the_same_time`; `unit/test_agents.py::TestManager::test_priorities_and_the_critical_slot` | `e2e/test_live_process.py::test_the_agents_build_and_verify_a_project_together` (two or more agents busy at once) |
| קבלת Result (receive a result) | `unit/test_agents.py::TestManager::test_an_agent_answers_with_the_whole_result_record`; `unit/test_orchestrator.py::TestVerification` (claimed files must exist; the reviewer sends work back) | `e2e/test_live_process.py::test_the_agents_build_and_verify_a_project_together` (the site's files on disk); `e2e/test_ui_browser.py::test_a_task_runs_in_the_window_from_plan_to_result` |
| Error Handling | `unit/test_agents.py::TestManager::test_a_crash_is_reported_and_the_agent_recovers`, `::test_timeout`, `::test_unknown_skill`, `TestBaseAgent::test_a_model_failure_falls_back_to_offline_work`; `unit/test_orchestrator.py::TestRecovery`; `unit/test_tools_executor.py::TestGate::test_unknown_tool_and_bad_input_fail_softly`; `unit/test_selfcheck.py::test_a_crashing_check_is_a_failed_line_not_a_crash` | `e2e/test_live_process.py::test_a_failing_project_is_reported_and_checked_again_after_the_fix` |
| Retry | `unit/test_orchestrator.py::TestRecovery::test_retry_with_feedback_then_success`, `::test_retry_limit_and_skipped_dependents`, `::test_the_same_error_stops_retrying`, `::test_no_retry_after_the_user_said_no`, `::test_a_crash_is_recovered_by_retrying`, `TestControl::test_retry_runs_the_failed_steps_again`, `TestHelpers::test_backoff_grows_and_is_capped` | `e2e/test_live_process.py::test_a_failing_project_is_reported_and_checked_again_after_the_fix` |
| Memory | `unit/test_memory.py` (short- and long-term memory, Hebrew search, projects, task checkpoints, no secrets stored); `unit/test_core.py::TestInstant::test_remember_recall_forget`; `unit/test_orchestrator.py::TestPlanningAndAI::test_the_plan_and_every_step_see_what_jarvis_remembers`; `integration/test_server.py::TestRest::test_memory` | `e2e/test_live_process.py::test_memory_and_unfinished_work_survive_a_crash_and_a_restart`; `jarvis check --full` |
| Voice | `unit/test_voice.py`: the pipeline (wake word, follow-up, barge-in, push to talk, errors), wake-word engines, speech recognition, text-to-speech, end-of-speech detection, the service; `TestRealModels` with the real models | `jarvis voice-test` and `jarvis check --voice` on the PC (a real microphone and speakers) |
| File Operations | `unit/test_tools_builtin.py::TestFiles` (write, read Hebrew and ANSI and .docx, copy, move, find, delete to the Recycle Bin, broad and permanent deletes ask first); `unit/test_security_review.py` (Windows device names, UNC paths, alternate data streams); `integration/test_server.py::TestRest::test_files` | `e2e/test_live_process.py::test_commands_act_on_the_computer_and_the_window_follows_live`; `e2e/test_ui_browser.py::test_the_window_shows_live_state_and_runs_commands` |
| Permission System | `unit/test_permissions.py`; `unit/test_tools_executor.py::TestConfirmation`, `TestGate`; `unit/test_security_review.py`; `unit/test_security_primitives.py`; `unit/test_core.py::TestFastPath::test_a_spoken_yes_is_not_enough_for_what_cannot_be_undone`; `ui/src/components/ConfirmModal.test.tsx` | `e2e/test_live_process.py::test_nothing_irreversible_happens_without_an_approval`; `e2e/test_ui_browser.py::test_approvals_and_the_kill_switch_in_the_window`; `jarvis check --full` |
| Task Cancellation | `unit/test_agents.py::TestManager::test_cancel_a_task_running_and_queued`, `::test_the_callers_cancel_token_reaches_the_agent`; `unit/test_orchestrator.py::TestControl::test_cancel_a_running_task`, `TestResume::test_cancel_an_interrupted_task`; `unit/test_tools_executor.py::TestRuntime::test_cancel_mid_run`, `TestConfirmation::test_cancelling_the_task_while_waiting_for_approval`; `unit/test_core.py::TestTasks::test_stop_the_task`, `::test_cancel_all_tasks` | `e2e/test_live_process.py::test_stopping_a_task_and_the_kill_switch` |
| UI updates | `integration/test_server.py::TestEvents`; `ui/src/state/reducer.test.ts` | `e2e/test_live_process.py::test_commands_act_on_the_computer_and_the_window_follows_live` (the WebSocket events); all of `e2e/test_ui_browser.py` |

The kill switches are covered with them: `unit/test_agents.py::TestManager::test_global_kill_switch`,
`unit/test_tools_executor.py::TestRuntime::test_kill_switch_before_and_during`,
`unit/test_orchestrator.py::TestControl::test_kill_switch_pauses_and_resume_continues`, and through
a running JARVIS in `e2e/test_live_process.py::test_stopping_a_task_and_the_kill_switch` and
`e2e/test_ui_browser.py::test_approvals_and_the_kill_switch_in_the_window`.

## The work screens: code, images, video, apps, privacy

The checks the work screens were asked to pass, and the tests that make them. The AI, xAI and the screen
are stand-ins in these tests (a scripted Grok, an `httpx.MockTransport` in place of xAI with a made-up key, a
fake desktop and computer-control model), so no test sends anything to a paid service; the shared fixtures
also remove any `XAI_API_KEY`, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` from the environment.

| Required check | Covered by |
|---|---|
| The app opens and the new screens load | `e2e/test_ui_browser.py::test_the_work_areas_show_what_is_real_and_ask_first` (every area from the rail, against a real JARVIS); `ui/src/screens/*/*.test.tsx` (each screen on its own, with the core's answers scripted) |
| A code request gets a result or a clear error | `integration/test_dev.py::TestCodeScreen::test_plan_review_apply_and_undo`, `::test_a_bad_answer_changes_nothing`, `::test_a_broken_proposal_is_shown_but_cannot_be_applied`, `::test_a_proposal_can_be_revised_in_words`; `ui/src/screens/code/code.test.tsx` |
| Running tests reports real results | `integration/test_dev.py::TestTests::test_tests_run_after_approval_and_a_fix_gets_the_failures` (a real pytest run: what ran, 1 passed, 1 failed, which test), `::test_a_project_without_tests_says_so`; the browser test above, in the window |
| Images and videos are made on this computer, free, with no paid AI | `unit/test_local_media.py` (the worker's protocol, memory plan and refusal, the engine: start, progress, stop midway, crash, out of memory, the memory watch, idle stop, turns on the graphics card; the installer's checks; the screens' jobs with a stand-in worker); `integration/test_local_media_real.py` (the real worker on tiny models: image, change, video, first-frame video, 8-bit); `integration/test_media_routes.py::TestLocalByDefault`; the local cases in `ui/src/screens/images/ImagesScreen.test.tsx` and `ui/src/screens/video/VideoScreen.test.tsx` |
| Image and video screens show whether the service is available | `integration/test_media_routes.py::TestMediaRoutes::test_status_says_what_is_missing`, `::test_creating_for_real_without_xai_is_refused_not_faked`, `::test_a_demonstration_end_to_end`, `::test_a_real_image_is_created_shown_and_saved_after_approval` (a fake xAI); `ui/src/screens/images/ImagesScreen.test.tsx`, `ui/src/screens/video/VideoScreen.test.tsx` |
| Actions can be stopped and cancelled | `integration/test_work.py::TestJobs::test_a_job_is_live_on_the_socket_stoppable_and_retryable`; `unit/test_jobs.py::TestJobs::test_stopping_a_job_abandons_its_wait_and_it_can_be_retried`; `unit/test_media.py::TestService::test_stopping_a_video_stops_asking_and_keeps_nothing`; `unit/test_apps.py::TestStep::test_stop_while_it_waits`, `::test_esc_twice_stops_it_while_jarvis_is_hidden`, `::test_the_kill_switch_stops_it` |
| App actions ask for permission and approval | `unit/test_apps.py::TestLook::test_asks_first_and_sees_nothing_before_the_approval`, `::test_a_no_stops_it_and_nothing_is_seen`, `::test_a_password_manager_in_front_is_not_looked_into`; `unit/test_apps.py::TestStep::test_a_password_field_is_never_typed_into_even_when_asked_directly`; `integration/test_apps_routes.py::TestAppsRoutes::test_look_suggest_and_step_wait_for_the_approval`, `::test_bad_requests_and_the_kill_switch` |
| Nothing is sent, deleted or changed before the approval | `integration/test_dev.py::TestCodeScreen::test_a_declined_approval_sends_nothing`, `::test_the_tool_never_reaches_grok_without_the_users_click`, `::test_an_edit_after_review_needs_a_new_approval`; `unit/test_apps.py::TestSuggest::test_send_is_previewed_and_asked_again`, `TestStep::test_the_rules_ask_again_for_a_send_and_a_no_stops_it`; `integration/test_privacy_routes.py` (deleting only on request, work in progress kept); the browser test above (tests run only after the approval; the privacy screen deletes on the second click) |
| Errors are clear and reveal no secret | `unit/test_media.py::TestClient::test_the_key_never_shows_in_an_error`, `TestService::test_xai_failures_reach_the_job_in_words_without_the_key`; `unit/test_devmode.py::TestMaskCode::test_secret_values_are_hidden_and_the_code_stays_readable`; `integration/test_privacy_routes.py::TestPrivacyRoutes::test_what_is_kept_is_counted_without_its_content` |

The UI tests run with `cd ui; npm test`; the browser test needs a Chromium-based browser (see
`JARVIS_TEST_BROWSER` above). In a cloud build container without Playwright's own Chromium,
`JARVIS_TEST_BROWSER=/opt/pw-browsers/chromium python -m pytest tests/e2e` runs it.

## Speed

`scripts/bench_local.py` measures how fast JARVIS answers on this computer, with nothing leaving it and
nothing on the desktop touched: a fresh temporary home, no AI provider, voice off.

```powershell
python scripts/bench_local.py                    # this checkout
python scripts/bench_local.py --src <other>/src  # another version, for a before/after comparison
python scripts/bench_local.py --json             # the numbers as JSON
```

It reports importing the code, building and starting the app, understanding a command, the time from each
everyday command to the first reply, and how many of a list of everyday wordings are understood on the spot.
To compare with an older version, check that version out into another folder (`git worktree add ../old
<commit>`) and pass its `src` with `--src`. While JARVIS runs, the same kind of numbers are kept for real
use (`GET /api/metrics`, and the Speed panel under History & settings > Work history), where they can be
saved as a named snapshot ("before") to compare with later.

## What only the PC can check

The image and video models themselves (about 70 GB, a 16 GB graphics card): `jarvis media status`, then
`jarvis media bench` (or `--modes gpu8,model8 --keep <folder>` to compare modes picture by picture) measures the
load, the first and second image and a 3-second video, with the peak graphics memory and RAM, after printing what
each mode needs against the memory free at that moment.

A build machine has no microphone, speakers, Start menu or WebView2, and may not reach the speech
models' download site, so these run on the user's Windows PC: `python -m pytest -m windows` (a
developer setup), `jarvis check --full --voice` (any installation), and `scripts\build.ps1`, which
runs the tests, builds `dist\JARVIS`, runs `jarvis-cli check --full` on the build and compiles the
installer.
