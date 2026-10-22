# Fix session expiry on token refresh

Sessions were expiring early because `refresh_token()` compared the expiry time
in seconds against a timestamp in milliseconds.

## Changes

- Fix the unit mismatch in `refresh_token` in `src/auth/session.py`
- Add `SessionStore.extend()` so a refresh keeps the session alive
- Log a warning when a refresh token is reused (see src/auth/logging.py)
- Tests in `tests/test_session.py`

## Out of scope

- The login form and `LoginView` are unchanged.
- Payments code (`src/payments/`) was not touched.
