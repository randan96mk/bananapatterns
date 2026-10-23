import React, { useEffect, useState } from 'react';
import * as api from '../services/api';

// Warnings and errors persisted by the backend logger.
export default function LogsPanel() {
  const [logs, setLogs] = useState([]);
  const [level, setLevel] = useState('');
  const load = () => api.getLogs(level).then(setLogs).catch(() => {});
  useEffect(() => { load(); }, [level]); // eslint-disable-line

  return (
    <div className="panel">
      <div className="row">
        <h3>Logs</h3>
        <select value={level} onChange={(e) => setLevel(e.target.value)}>
          <option value="">Warnings + errors</option><option>WARNING</option><option>ERROR</option>
        </select>
        <button className="btn ghost" onClick={load}>Refresh</button>
        <span className="muted">Full INFO log: logs/bananapatterns.log</span>
      </div>
      <table className="grid small">
        <thead><tr><th>Time</th><th>Level</th><th>Source</th><th>Message</th></tr></thead>
        <tbody>{logs.map((l) => (
          <tr key={l.id}><td>{l.timestamp.replace('T', ' ').slice(0, 19)}</td>
            <td className={`lvl-${l.level}`}>{l.level}</td><td>{l.source}</td><td>{l.message}</td></tr>))}
        </tbody>
      </table>
    </div>
  );
}
