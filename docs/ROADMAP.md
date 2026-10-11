# Roadmap

## Alpha hardening

- [x] Add Python project metadata, editable install and CLI entry point.
- [x] Add safe sample configuration and git exclusions for user data.
- [x] Add offline unit tests and GitHub Actions CI.
- [x] Scope authenticated download requests to their trusted origin.
- [ ] Test real install/login/sync flows on a fresh macOS machine.
- [ ] Add a non-editable install config/data directory and first-run setup wizard.
- [ ] Test real university API failure and throttling scenarios.
- [ ] Document data-retention/permission boundaries by source.

## Beta usability

- [ ] Add integration test fixtures and coverage for Canvas, Ed and Echo360.
- [ ] Modularise the current single Python source file.
- [ ] Support predictable resume/incremental sync to reduce repeated downloads.
- [ ] Add progress display and useful actionable error messages.
- [ ] Add Windows and Linux integration testing.
- [ ] Introduce a `usync init`/`usync doctor` onboarding flow.
- [ ] Consider optional native schedule support and safe token storage.

## Public release

- [ ] Demonstrate a clean first-time install without editing code.
- [ ] Publish supported platforms/source integrations and known limitations.
- [ ] Define release and backwards-compatibility rules.
- [ ] Release signed/tagged source distribution after CI and manual verification.
