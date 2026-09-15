---
name: manifest
description: What to do when a tool result ends with "Manifest prepared corrected arguments"
---

# Manifest tool repair

When a tool call is rejected and its result ends with a line saying Manifest prepared corrected arguments, call the same tool again with exactly the same arguments you used. The corrected arguments are applied automatically on that retry; you do not need to change anything yourself.

If the retry is rejected as well, treat it as a normal error and do not retry again on Manifest's account. Only calls to external services are repaired; a local tool failure is always yours to fix.

If the user asks why a call was retried, say that Manifest repaired the rejected arguments based on the API's error.
