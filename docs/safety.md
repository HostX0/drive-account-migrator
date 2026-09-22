# Safety model and recovery

## Copy invariants

The source connection is read-only. Copies run as the destination account. Existing destination files are neither edited nor deleted. Every folder listing is paginated to the end; incomplete results, duplicate page IDs, repeated page tokens and permission errors are not treated as empty folders.

Mappings use source and destination IDs. Unmapped duplicate-name groups are conflicts unless every item is genuinely missing. Distinct source folders are never merged. An unmapped, unique binary candidate may be adopted only with matching metadata, size and available hashes, including MD5. Unmapped folders and native files are never adopted just by name.

A durable SQLite intent precedes each create/copy. A returned destination ID is saved immediately. Copies carry a unique `migrationIntent` app property. After a lost response, the worker lists the intended parent and looks for the one matching intent or saved ID. Zero or multiple candidates stop further attempts for that source; it does not assume failure and copy again.

Every new object is checked for name, MIME type, intended parent, destination ownership and available binary checksums. Optional source Editor access is checked using complete permission listings. Native metadata equality is not content equality; native content is not certified for cleanup.

## Controller coordination

The native Docs controller is read live with `includeTabsContent`. All text tabs, including nested tabs, are parsed together. Duplicate JSON keys, unsupported content or missing revisions stop the run. The writer currently requires one nonempty text tab.

A fresh revision plus `writeControl.requiredRevisionId` is used for lease acquisition and every checkpoint. Revision conflicts are not retried blindly. A readback verifies the expected controller text. Stop states, `apps_script` runner mode, nonexpired foreign leases, nested mappings and blocked records are honored. Only the current run's own lease may be released.

Controller checkpoints are appended; local SQLite is not replaced by chat history. Copy checkpoints occur at least every ten successful copies and at shutdown. Cleanup checkpoints occur after every verified item. Near the document-size limit the public worker stops; this release does not automatically compact historical records. Back up the controller and state before an expert-supported archival procedure. Never discard unresolved mappings or exclusions to make it fit.

The OS lock prevents two local processes sharing a state directory. The remote lease protects cooperating workers using the same controller. It cannot protect against an unrelated client that ignores the protocol or a person editing files concurrently. Keep the source and destination quiescent while copying or cleaning up; verification narrows, but cannot eliminate, concurrent-edit races.

## Cleanup invariants

A cleanup plan is an immutable reviewed snapshot identified by SHA-256. The source cleanup token is separate from the read-only source token. Live checks require distinct IDs, expected owners, mapped destination, source-root/destination-root ancestry, exact metadata, binary hashes and unchanged versions. Source access is required only when `share_source` is enabled.

Trash accepts only binaries. Purge additionally requires a previously verified Trash record and the exact post-trash source version. Folders are never recursively deleted. The tool does not delete destinations, revoke permissions, empty unrelated Trash or operate on Gmail/Photos.

An action intent is committed before a mutation. No mutation request is automatically retried. Purge success requires an accepted delete response followed by source GET 404 and an unchanged readable destination. A 404 by itself is insufficient evidence that this worker deleted a file. If the connection fails at an uncertain point, the record remains uncertain and subsequent runs stop instead of sending another deletion.

A verified local result survives a later controller-checkpoint failure; it is published to the controller on resume. Keep both the database and controller. API restrictions are not bypassed.

## If something stops

1. Preserve the state directory, controller and reports. Do not create another mirror.
2. Read the exception and distinguish a bounded run, lease conflict, changed item, permission restriction and uncertain network result.
3. A normal bounded copy run can be resumed with the same command. Let any live lease finish or expire first.
4. A changed or ambiguous item stays retained. Inspect it manually; never overwrite it to force a match.
5. An uncertain cleanup operation needs a deliberate read-only reconciliation of its recorded IDs. This release provides evidence in SQLite but **does not automatically resolve uncertain cleanup records**. Do not edit states merely to get another deletion attempt.
6. Keep blocked and unsupported IDs in the configuration/controller. Never remove a restriction because a retry might work.

This is a conservative migration utility, not a guarantee that every Google account object or sharing relationship can be reproduced. A completed accessible traversal is not a claim of global completion.
