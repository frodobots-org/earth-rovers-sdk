# RoboColosseum sidecar

Run one SDK process per intended rover, bound to localhost. Configure `BOT_SLUG`, a dedicated SDK-enabled open `MISSION_SLUG` with zero checkpoints, a non-admin organizer account and `CONTROL_WATCHDOG_S=0.5`. Keep the existing API authentication configuration. The Colosseum runner starts one ride, executes several evaluation windows, then ends the ride when the operator selects End session.

`POST /start-mission` now returns `identity`, copied from the FrodoBots API's additive `ROVER_IDENTITY` field. `/status` returns the same exact bot ID/slug, device reference, ride ID, recording status/prefix and SDK version, without Agora or API tokens. Identity comes from the acquired ride; it is never inferred from ride history or timestamps.

The runner calls `/start-mission?evaluation=true`. The SDK forwards that flag before acquiring the ride; the updated API rejects admin takeover and personal-mode recording for evaluations. This mode cannot bypass ride identity by using static Agora environment tokens.

`POST /stop` waits for the existing confirmed-stop path without ending the ride. A successful response has `stop_requested=true`, `stop_confirmed=true`, `stationary_verified=false`. Confirmation is a transport receipt; the Colosseum runner separately verifies fresh speed and wheel RPM telemetry during the required stationary hold. Failed confirmation returns an error. `/end-mission` retains its existing confirmed-stop requirement.

Deploy the API identity addition before using the new runner. Older SDK clients ignore the new response fields. The new runner refuses missing identity, personal-mode recording, admin accounts, stale camera/telemetry or an excessive watchdog setting. Qualify physical stopping on network loss before supervised evaluation. An API receipt alone does not prove the robot stopped.
