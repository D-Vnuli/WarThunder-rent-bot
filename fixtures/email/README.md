# Sanitized email fixtures

No real Gmail messages are stored in this repository. Before enabling a production
classifier policy, the owner must add sanitized/anonymized `.eml` samples for the
approved LOGIN_OTP and PASSWORD_CHANGE formats. Remove email addresses, user IDs,
reset tokens, personal URLs, security identifiers, and every other sensitive value
while preserving only the verified structural markers needed by the allowlist.
