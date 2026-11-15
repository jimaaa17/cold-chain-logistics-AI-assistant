import { useState, useCallback } from "react";
import { fetchAuditTrail, fetchLiveTraces } from "../../../shared/api/endpoints.js";

export function useAuditLogs() {
  const [rows, setRows] = useState([]);
  const [liveSpans, setLiveSpans] = useState([]);
  const [observability, setObservability] = useState(null);
  const [authenticated, setAuthenticated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [credentials, setCredentials] = useState({ username: "", password: "" });

  const loadLogs = useCallback(
    async ({ username, password, sessionIdFilter = null, nodeFilter = "ALL" }) => {
      setLoading(true);
      setError(null);
      try {
        const result = await fetchAuditTrail({
          username,
          password,
          sessionIdFilter,
          nodeFilter,
        });
        setRows(result.rows || []);
        setAuthenticated(true);
        setCredentials({ username, password });
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    },
    []
  );

  const loadLiveTraces = useCallback(
    async ({ sessionId = null, nodeFilter = "ALL" } = {}) => {
      setLoading(true);
      setError(null);
      try {
        const result = await fetchLiveTraces({ sessionId, nodeFilter });
        setLiveSpans(result.spans || []);
        if (result.observability) {
          setObservability(result.observability);
        }
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    },
    []
  );

  return {
    rows,
    liveSpans,
    observability,
    authenticated,
    loading,
    error,
    credentials,
    loadLogs,
    loadLiveTraces,
  };
}
