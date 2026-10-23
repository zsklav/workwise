# Authentication and authorization

WorkWise uses username/password signup and login, Argon2 password hashes, and
HS256 JWT access tokens. Authorization uses the current database user and the
company's stored owner. Jobs inherit their company's permissions.

## Endpoints

| Method | Path | Input | Result |
|---|---|---|---|
| POST | `/auth/signup` | JSON username/password | Member user, 201 |
| POST | `/auth/login` | Password-grant form | Bearer token, 200 |
| POST | `/auth/refresh` | JSON refresh token | Rotated access/refresh pair, 200 |
| POST | `/auth/logout` | Current access bearer | Revoke all account sessions, empty 204 |
| GET | `/auth/me` | Bearer token | Current user, 200 |

Signup usernames are stripped and lowercased, then validated as 3–50 ASCII
letters/digits/underscores. Database constraints enforce this canonical form
and uniqueness. Duplicate usernames return 409. Passwords retain their exact
input and must contain 15–128 characters. pwdlib hashes them with Argon2;
plaintext passwords and stored hashes are absent from user responses.
Authentication validation errors remove input/context values so they cannot
echo credentials, including when a whole request body fails validation.

Signup accepts only username/password and always creates an active member.
Clients cannot supply role, ID, active status, or company ownership. Login
accepts `application/x-www-form-urlencoded` fields `grant_type=password`,
`username`, and `password`. Missing accounts and incorrect passwords share a
401 response; unknown accounts still verify a dummy password hash. Disabled
accounts cannot log in. Token responses set `Cache-Control: no-store`.

## Tokens and configuration

The ignored local `.env` contains a generated JWT_SECRET; `.env.example`
contains a placeholder. Configure a random secret of at least 32 characters.
Placeholder secrets are rejected. The algorithm is fixed to HS256 and is not
chosen from token input. Tokens contain string user-ID `sub`, integer
`iat`/`nbf`/`exp`, issuer, audience, and access-token type. All are required and
verified. Default lifetime is 30 minutes (configuration range 1–1440).

Each protected request verifies the signature and claims, then loads the user
from PostgreSQL. Missing or inactive users return 401 with a Bearer challenge.
Role is read from the database, never a token claim. Database role changes and
deactivation therefore apply on subsequent requests without issuing a new
token. There is no individual-device revocation UI. Refresh rotation and account-wide logout now
use persisted token hashes and user epochs. JWTs require an integer `ver`
matching the current user epoch. Login/signup/refresh have shared PostgreSQL
rate limits. See [security hardening](security-hardening.md) for exact contracts,
replay containment, retry behavior and the required sign-in after upgrade.

## Permissions

| Action | Anonymous | Member | Admin |
|---|---|---|---|
| Health, list/get jobs or companies | Allowed | Allowed | Allowed |
| Read `/auth/me` | 401 | Own account | Own account |
| Read `/metrics` | 401 | 403 | Allowed |
| Create company | 401 | Own company | Own company |
| Edit/delete company | 401 | Owned companies | Any company |
| Create/edit/delete job | 401 | Jobs in owned companies | Any company |
| Move job between companies | 401 | Must own both companies | Allowed |
| Manage legacy unowned company/jobs | 401 | 403 | Allowed |

The server sets owner_id when creating a company. Ownership is immutable
through public write schemas. Public Company responses retain their existing
fields without exposing owner IDs. There is no ownership transfer/member-team
API at this checkpoint. An empty PATCH also requires ownership. Valid writes
targeting missing resources return 404; an existing unauthorized resource
returns 403. Admin access does not bypass validation, uniqueness, or the 409
restriction on deleting a company with linked jobs.

## Existing data and admin bootstrap

Revision 0005 adds users and nullable company owner_id. Legacy companies remain
unowned and are manageable only by admins. No default accounts, passwords, or
first-signup promotion are created. After migration and local configuration:

```powershell
python -m workwise.cli.manage_users create-admin workwise_admin
```

This local operator command uses migration credentials and prompts with
`getpass` twice. It creates a new admin with an Argon2 hash. Duplicate usernames
fail rather than promoting an existing account. Role changes/deactivation are
trusted database-operator actions; there is no public role-management endpoint.

Runtime grants are SELECT/INSERT on users, UPDATE only on their token_version
column, SELECT/INSERT/UPDATE on auth state, and CRUD on companies/jobs. The
readiness endpoint additionally has SELECT on Alembic's version table. The
runtime role has no role/password update, user DELETE or schema-management grant. API ownership
checks are application authorization, not PostgreSQL row-level security;
database credentials must remain private. Configure HTTPS when deploying bearer
credentials. Account recovery, MFA and deployment hardening are later
work; this checkpoint is verified locally and is not a production security audit.

## Verification

The PostgreSQL suite has 219 passing cases, including real signup/login,
password hash verification, malformed/expired/incorrectly signed JWT rejection,
role-claim rejection as an authority source, disabled accounts, all unauthenticated
write routes, cross-owner write denial, both sides of job movement, administrator
access, and legacy records. Existing persistence tests now sign up and log in
before exercising real commits. A CLI integration test creates and cleans up a
test admin. Migration upgrade/downgrade tests preserve legacy data without
assigning owners. No concurrent security test or brute-force benchmark was run.

## References

- [FastAPI password hashing and JWT tutorial](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)
- [PyJWT verification API](https://pyjwt.readthedocs.io/en/stable/api.html)
- [OWASP authorization guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)
