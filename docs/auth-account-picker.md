# Authentication account picker

Telegram's bare `/auth` command opens Hermes' native account picker. The command is handled by
the gateway and Telegram adapter; it is never sent to the language model.

The root view shows the active account and every configured credential-pool account. It includes
only a sanitized label, provider, non-reversible account/credential fingerprint, verified
credential plan claim when available, and current pool availability. Multiple grants that carry
the same provider account identity are grouped as aliases. Email-shaped and token-shaped labels
are replaced with the account fingerprint. Tokens, refresh tokens, emails, OAuth device codes,
and credential IDs are absent from button callback data and account menus.

Selecting an account opens `Use`, `Reauthenticate`, and `Back`. `Use` requires a second explicit
confirmation, then moves the selected native pool entry to first priority and invalidates idle
cached agents for that provider. An already-running turn keeps the credential it started with;
use `/new` after that turn only when an immediate switch is required. A gateway restart is not
required.

`Add account` and `Reauthenticate` call the provider's existing OAuth or device-code flow outside
model context. These actions, including `Use`, are available only to gateway admins in a private
chat. A group invocation returns a private-chat handoff without enumerating accounts or starting
authentication. Provider verification details may be delivered only to the bound private chat
when the provider-native flow exposes that callback.

Picker callbacks use a short-lived opaque nonce and are bound to the authorized requesting user,
chat, topic, message, and gateway session state. A mutation consumes its nonce before execution;
expired, cross-user, cross-chat, cross-topic, and replayed callbacks fail closed.

Typed operations remain available:

```text
/auth status
/auth use <provider> <account-index-or-id-or-label>
/auth reauth <provider> [account-index-or-id-or-label]
/auth add <provider>
```

The CLI exposes the same native operations as `hermes auth status`, `hermes auth use`,
`hermes auth reauth`, and `hermes auth add`.
