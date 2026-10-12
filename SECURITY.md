# Security reporting and protected inputs

Do not post credentials, customer data or exploit details in a public issue.
Use GitHub private vulnerability reporting when enabled for this repository.
For an accidentally exposed credential, revoke or rotate it before treating
removal from the current tree as sufficient. Git history and cached copies can
retain removed data.

Operational reports and filled configuration stay in protected storage. Public
fixtures must be inert, documented and independently reviewed. An exception to
secret scanning must match an exact rule, file, blob hash and candidate hash;
blanket path/rule exclusions are not permitted.
