// Wipes every client-side cache that holds one specific user's data. Called from
// AuthContext whenever the signed-in user changes (login / logout / account
// switch) so a second account never sees the first's cached renders on a shared
// session or device. The backend already scopes every query by uid — this only
// fixes cache *lifecycle* on the client.
import { resetPastCache } from "../hooks/useJobs";
import { clearAllTerminalDetail } from "../utils/terminalDetailCache";
import { clearRetriedJobs } from "../utils/retriedJobsCache";

export function clearUserScopedCaches() {
  resetPastCache(); // module-scope past render-group list
  clearAllTerminalDetail(); // localStorage: pcrent:detail:v1:*
  clearRetriedJobs(); // localStorage: pcrent:retried_chunk_jobs:v1
}
