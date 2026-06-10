import { useState, useEffect, useRef, useCallback } from 'react';
import {
  Shield,
  Cpu,
  Database,
  Activity,
  Terminal,
  XOctagon,
  RefreshCw,
  Server,
  Network,
  Zap,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Search,
  Wrench,
  Eye,
} from 'lucide-react';
import './App.css';

// ---- Types -----------------------------------------------------------------
interface SwarmEvent {
  id: number;
  timestamp: string;
  type: string;
  agent: string;
  content: string;
  tool?: string;
  args?: Record<string, unknown>;
  command?: string;
  success?: boolean;
  action?: string;
  target?: string;
  reason?: string;
  evidence?: string[];
  approved?: boolean;
  risk_level?: string;
  safety_checks?: Array<{ check: string; passed: boolean; detail: string }>;
  total_heal_time?: number;
  metric?: string;
  pod?: string;
  mode?: string;
  provider?: string;
  model?: string;
  severity?: string;
  status?: string;
  phase?: string;
  turn?: number;
  poll_count?: number;
  remaining?: number;
}

// ---- SVG DAG Config --------------------------------------------------------
const NODES = {
  network: { id: 'network', label: 'L3/L4 Network', x: 100, y: 80, icon: Network, desc: 'TCP Retransmits & Connect' },
  cpu: { id: 'cpu', label: 'cgroup CPU', x: 300, y: 55, icon: Cpu, desc: 'cpu.max throttle probes' },
  mem: { id: 'mem', label: 'cgroup Memory', x: 300, y: 185, icon: Database, desc: 'memory.max cgroup tracking' },
  exit: { id: 'exit', label: 'Process Exit', x: 500, y: 120, icon: XOctagon, desc: 'sched_process_exit hooks' },
  brain: { id: 'brain', label: 'Causal Brain', x: 700, y: 120, icon: Activity, desc: 'PC-PR Causal Discovery' },
  swarm: { id: 'swarm', label: 'Agent Swarm', x: 900, y: 120, icon: Shield, desc: 'Planner→Evaluator→Executor' },
};

// ---- High-Density Metric Bar Component --------------------------------------
function MetricBar({
  label,
  value,
  max,
  unit,
  color,
  dangerThreshold,
}: {
  label: string;
  value: number;
  max: number;
  unit: string;
  color: string;
  dangerThreshold: number;
}) {
  const pct = Math.min((value / max) * 100, 100);
  const isDanger = value >= dangerThreshold;
  
  return (
    <div className="flex flex-col gap-1 py-1">
      <div className="flex items-center justify-between text-[11px]">
        <span className="font-mono font-semibold text-zinc-400 uppercase tracking-wider">{label}</span>
        <div className="flex items-baseline gap-1 font-mono">
          <span className={`font-bold ${isDanger ? 'text-red-400' : 'text-zinc-100'}`}>
            {typeof value === 'number' ? (value % 1 === 0 ? value : value.toFixed(1)) : value}
          </span>
          <span className="text-[10px] text-zinc-500">{unit}</span>
        </div>
      </div>
      <div className="h-1.5 w-full bg-zinc-950 border border-[#222226] rounded-sm overflow-hidden">
        <div 
          className="h-full transition-all duration-500 ease-out"
          style={{ 
            width: `${pct}%`,
            backgroundColor: isDanger ? '#ef4444' : color 
          }}
        />
      </div>
    </div>
  );
}

// ---- Agent Color Map Helper ------------------------------------------------
const getAgentColor = (agent: string) => {
  const colors: Record<string, string> = {
    planner: '#818cf8', // Indigo
    evaluator: '#34d399', // Emerald
    executor: '#60a5fa', // Blue
    system: '#a1a1aa', // Slate
  };
  return colors[agent] || '#71717a';
};

// ---- Event Message Renderer ------------------------------------------------
function EventMessage({ event }: { event: SwarmEvent }) {
  const renderContent = () => {
    switch (event.type) {
      case 'incident':
        return (
          <div className="p-2.5 rounded border border-red-950/40 bg-red-950/10">
            <div className="flex items-center gap-2 mb-1.5">
              <AlertTriangle className="w-3.5 h-3.5 text-red-400" />
              <span className="text-red-400 font-bold text-[10px] uppercase tracking-wider">CRITICAL INCIDENT DETECTED</span>
              <span className="badge badge-danger text-[9px]">{event.severity || 'critical'}</span>
            </div>
            <p className="text-zinc-300 text-xs font-mono leading-relaxed">{event.content}</p>
          </div>
        );

      case 'config':
        return (
          <div className="p-2 rounded border border-zinc-800 bg-zinc-900/10 text-zinc-400 text-xs font-mono">
            {event.content} {event.model && <span className="text-zinc-500 font-normal">({event.model})</span>}
          </div>
        );

      case 'agent_start':
        return (
          <div className="text-zinc-400 text-xs font-mono">
            {event.content}
          </div>
        );

      case 'thinking':
        return (
          <div className="flex items-center gap-2 py-1 text-zinc-400 text-xs font-mono">
            <Search className="w-3.5 h-3.5 text-zinc-500 animate-pulse" />
            <span>{event.content}</span>
            <div className="typing-indicator">
              <span></span><span></span><span></span>
            </div>
          </div>
        );

      case 'tool_call':
        return (
          <div className="p-2.5 rounded border border-amber-950/30 bg-amber-950/5">
            <div className="flex items-center gap-1.5 mb-1.5">
              <Wrench className="w-3.5 h-3.5 text-amber-500" />
              <span className="text-amber-500 font-bold text-[9px] uppercase tracking-wider font-mono">Tool Call</span>
            </div>
            <pre className="agent-code text-amber-200/80">
              <code>$ {event.command || event.content}</code>
            </pre>
          </div>
        );

      case 'tool_result':
        return (
          <div className="p-2.5 rounded border border-zinc-800 bg-zinc-950">
            <div className="flex items-center gap-2 mb-1.5">
              <Terminal className="w-3.5 h-3.5 text-zinc-400" />
              <span className="text-zinc-300 font-bold text-[9px] uppercase tracking-wider font-mono">Execution Output</span>
              {event.success !== undefined && (
                <span className={`badge ${event.success ? 'badge-success' : 'badge-danger'} text-[8px]`}>
                  {event.success ? 'STATUS: OK' : 'STATUS: ERR'}
                </span>
              )}
            </div>
            <pre className="agent-code text-zinc-300 max-h-48 overflow-y-auto">
              <code>{event.content}</code>
            </pre>
          </div>
        );

      case 'reasoning':
        return (
          <div className="p-3 rounded border border-indigo-950/20 bg-indigo-950/5">
            <div className="flex items-center gap-1.5 mb-2">
              <Eye className="w-3.5 h-3.5 text-indigo-400" />
              <span className="text-indigo-400 font-bold text-[9px] uppercase tracking-wider font-mono">SRE Engine Reasoning</span>
            </div>
            <div className="text-zinc-300 text-xs font-mono leading-relaxed whitespace-pre-wrap">{event.content}</div>
          </div>
        );

      case 'decision':
        return (
          <div className="p-3 rounded border border-indigo-950/40 bg-indigo-950/10">
            <div className="flex items-center gap-2 mb-2">
              <Zap className="w-3.5 h-3.5 text-indigo-400" />
              <span className="text-indigo-300 font-bold text-[10px] uppercase tracking-wider font-mono">REMEDIATION DECISION</span>
              <span className="badge badge-warning text-[9px]">{event.action}</span>
            </div>
            <p className="text-zinc-300 text-xs font-mono leading-relaxed mb-2">{event.reason || event.content}</p>
            {event.evidence && event.evidence.length > 0 && (
              <div className="mt-2 flex flex-col gap-1 border-t border-indigo-950/40 pt-2">
                <span className="text-[9px] text-zinc-500 font-bold font-mono uppercase">Diagnosis Evidence:</span>
                {event.evidence.map((e, i) => (
                  <div key={i} className="flex items-start gap-1.5 text-[11px] font-mono text-zinc-400">
                    <span className="text-indigo-400">•</span>
                    <span>{e}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        );

      case 'evaluation':
        return (
          <div className={`p-3 rounded border ${event.approved ? 'border-emerald-950/40 bg-emerald-950/5' : 'border-red-950/40 bg-red-950/5'}`}>
            <div className="flex items-center gap-2 mb-2">
              {event.approved ? (
                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
              ) : (
                <XCircle className="w-3.5 h-3.5 text-red-400" />
              )}
              <span className={`font-bold text-[10px] uppercase tracking-wider font-mono ${event.approved ? 'text-emerald-400' : 'text-red-400'}`}>
                {event.approved ? 'Audit: Action Approved' : 'Audit: Action Blocked'}
              </span>
              {event.risk_level && (
                <span className={`badge ${event.risk_level === 'low' ? 'badge-success' : event.risk_level === 'medium' ? 'badge-warning' : 'badge-danger'} text-[8px]`}>
                  Risk: {event.risk_level}
                </span>
              )}
            </div>
            <p className="text-zinc-300 text-xs font-mono leading-relaxed mb-2">{event.content}</p>
            {event.safety_checks && event.safety_checks.length > 0 && (
              <div className="flex flex-col gap-1.5 mt-2.5 pl-3">
                {event.safety_checks.map((sc, i) => (
                  <div key={i} className={`safety-check ${sc.passed ? 'passed' : 'failed'}`}>
                    {sc.passed ? (
                      <CheckCircle2 className="w-3 h-3 text-emerald-400 shrink-0" />
                    ) : (
                      <XCircle className="w-3 h-3 text-red-400 shrink-0" />
                    )}
                    <span className="font-semibold font-mono text-[10px]">{sc.check}:</span>
                    <span className="text-zinc-400 font-mono text-[10px]">{sc.detail}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        );

      case 'executing':
      case 'command_output':
        return (
          <div className="p-2.5 rounded border border-emerald-950/30 bg-emerald-950/5">
            <div className="flex items-center gap-1.5 mb-1.5">
              <Terminal className="w-3.5 h-3.5 text-emerald-500" />
              <span className="text-emerald-500 font-bold text-[9px] uppercase tracking-wider font-mono">
                {event.type === 'executing' ? 'Executing Remediation Command' : 'Command Standard Output'}
              </span>
            </div>
            <pre className="agent-code text-emerald-200/80">
              <code>{event.command || event.content}</code>
            </pre>
          </div>
        );

      case 'verifying':
        return (
          <div className="flex items-center gap-2 py-1 text-emerald-400 text-xs font-mono">
            <RefreshCw className="w-3.5 h-3.5 text-emerald-500 animate-spin" />
            <span>{event.content}</span>
          </div>
        );

      case 'verified':
        return (
          <div className="p-2.5 rounded border border-emerald-950/40 bg-emerald-950/10">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
              <span className="text-emerald-400 font-bold text-[10px] uppercase tracking-wider font-mono">VERIFICATION PASSED</span>
              <span className="badge badge-success text-[8px]">Pod Healthy</span>
            </div>
            <pre className="agent-code text-emerald-200/80 mt-2">
              <code>{event.content}</code>
            </pre>
          </div>
        );

      case 'healed':
        return (
          <div className="p-3 rounded border border-emerald-500/30 bg-emerald-950/20">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded border border-emerald-500/20 bg-emerald-500/10 flex items-center justify-center">
                <CheckCircle2 className="w-4 h-4 text-emerald-400" />
              </div>
              <div>
                <span className="text-emerald-400 font-bold text-xs font-mono block">Self-Healing Execution Complete ✓</span>
                <span className="text-zinc-400 text-[10px] font-mono">
                  Remediation loop resolved in <strong className="text-emerald-400 font-semibold">{event.total_heal_time} seconds</strong>.
                </span>
              </div>
            </div>
          </div>
        );

      case 'warning':
        return (
          <div className="p-2.5 rounded border border-amber-950/30 bg-amber-950/5">
            <div className="flex items-center gap-1.5 text-amber-400 text-xs font-mono">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
              <span>{event.content}</span>
            </div>
          </div>
        );

      case 'error':
        return (
          <div className="p-2.5 rounded border border-red-950/30 bg-red-950/5">
            <div className="flex items-center gap-1.5 text-red-400 text-xs font-mono">
              <XCircle className="w-3.5 h-3.5 shrink-0" />
              <span>{event.content}</span>
            </div>
          </div>
        );

      default:
        return (
          <p className="text-zinc-400 text-xs font-mono leading-relaxed p-2.5 rounded border border-zinc-800 bg-zinc-950">
            {event.content}
          </p>
        );
    }
  };

  return (
    <div className={`flex gap-0 agent-msg ${event.agent}`}>
      {/* Left Column: Timestamp & Continuous Vertical Timeline Line */}
      <div className="w-18 shrink-0 text-right pr-4 py-1.5 relative select-none">
        {/* Continuous vertical timeline track segment */}
        <div className="absolute right-0 top-0 bottom-0 w-[1.5px] bg-[#222226]" />
        {/* Monospace Agent Dot */}
        <div 
          className="absolute right-[-4px] top-[12px] w-2.5 h-2.5 rounded-full border border-zinc-950 z-10 transition-colors duration-300"
          style={{ backgroundColor: getAgentColor(event.agent) }}
        />
        <span className="font-mono text-[9px] text-zinc-500 font-medium tracking-tight">
          {event.timestamp?.split('T')[1] || 'now'}
        </span>
      </div>

      {/* Right Column: Component Badge & Log Content */}
      <div className="flex-1 pl-4 py-1.5 pb-4 min-w-0 flex flex-col gap-1.5">
        <div className="flex items-center gap-2 text-[10px]">
          <span className="font-mono font-bold uppercase tracking-wider" style={{ color: getAgentColor(event.agent) }}>
            [{event.agent.toUpperCase()}]
          </span>
          <span className="text-[9px] text-zinc-600 font-mono font-medium tracking-tight uppercase">
            {event.type}
          </span>
        </div>
        <div className="min-w-0">
          {renderContent()}
        </div>
      </div>
    </div>
  );
}

// ---- Main App ==============================================================
function App() {
  const [activeFault, setActiveFault] = useState<string | null>(null);
  const [isSimulating, setIsSimulating] = useState(false);
  const [events, setEvents] = useState<SwarmEvent[]>([]);
  const [apiConnected, setApiConnected] = useState(false);
  const [aiConfig, setAiConfig] = useState<{ provider: string; model: string; mode: string } | null>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  // Metrics state
  const [metrics, setMetrics] = useState({
    cpu: 14.8,
    memory: 114,
    tcpRetransmits: 0.1,
    restarts: 0,
    healTime: 0,
    podName: 'victim-app-simulation-76d9bf84c5-hj9qw',
  });

  // Fetch AI config on mount
  useEffect(() => {
    const fetchConfig = async () => {
      try {
        const res = await fetch('http://localhost:5000/api/config');
        if (res.ok) {
          const data = await res.json();
          setAiConfig(data);
          setApiConnected(true);
        }
      } catch {
        setApiConnected(false);
      }
    };
    fetchConfig();
    const interval = setInterval(fetchConfig, 10000);
    return () => clearInterval(interval);
  }, []);

  // Poll status for metrics
  useEffect(() => {
    const poll = async () => {
      try {
        const res = await fetch('http://localhost:5000/api/status');
        if (!res.ok) return;
        const data = await res.json();
        setApiConnected(true);

        // Update metrics from backend diagnosis state
        if (data.diagnoses && data.diagnoses.length > 0) {
          const lastDiag = data.diagnoses[data.diagnoses.length - 1];
          const isHealed = data.events?.some((e: SwarmEvent) => e.type === 'healed');

          if (isHealed) {
            setMetrics(prev => ({
              ...prev,
              cpu: 14.8,
              memory: 114,
              tcpRetransmits: 0.1,
              restarts: data.diagnoses.length,
              healTime: data.events?.find((e: SwarmEvent) => e.type === 'healed')?.total_heal_time || prev.healTime,
              podName: prev.podName,
            }));
            setIsSimulating(false);
            setActiveFault(null);
          } else if (isSimulating) {
            setMetrics(prev => ({
              ...prev,
              cpu: lastDiag.metric === 'cpu_spike' ? 91.2 : 14.8,
              memory: lastDiag.metric === 'memory_leak' ? 238 : 114,
              tcpRetransmits: lastDiag.metric === 'network_partition' ? 48.6 : 0.1,
              restarts: Math.max(data.diagnoses.length - 1, 0),
              podName: prev.podName,
            }));
          }
        }

        // Sync events from status poll (backup for SSE)
        if (data.events && data.events.length > 0) {
          setEvents(prev => {
            if (data.events.length > prev.length) return data.events;
            return prev;
          });
        }
      } catch {
        setApiConnected(false);
      }
    };

    poll();
    const interval = setInterval(poll, 2000);
    return () => clearInterval(interval);
  }, [isSimulating]);

  // SSE connection for real-time events
  const connectSSE = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }
    try {
      const es = new EventSource('http://localhost:5000/api/events');
      eventSourceRef.current = es;

      es.onmessage = (e) => {
        try {
          const evt: SwarmEvent = JSON.parse(e.data);
          if (evt.type === 'connected') return;
          setEvents(prev => {
            if (prev.some(p => p.id === evt.id)) return prev;
            return [...prev, evt];
          });
        } catch { /* ignore parse errors */ }
      };

      es.onerror = () => {
        es.close();
        setTimeout(connectSSE, 3000);
      };
    } catch { /* SSE not available */ }
  }, []);

  useEffect(() => {
    connectSSE();
    return () => {
      eventSourceRef.current?.close();
    };
  }, [connectSSE]);

  // Auto-scroll chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [events]);

  // Trigger chaos event
  const injectFault = async (type: string) => {
    if (isSimulating) return;
    setIsSimulating(true);
    setActiveFault(type);
    setEvents([]);

    // Set metrics to spiking state immediately
    if (type === 'cpu') setMetrics(prev => ({ ...prev, cpu: 91.2 }));
    if (type === 'memory') setMetrics(prev => ({ ...prev, memory: 238 }));
    if (type === 'network') setMetrics(prev => ({ ...prev, tcpRetransmits: 48.6 }));

    try {
      const res = await fetch(`http://localhost:5000/api/chaos?type=${type}`, { method: 'POST' });
      if (!res.ok) throw new Error('API error');
    } catch {
      setEvents([{
        id: 1,
        timestamp: new Date().toISOString(),
        type: 'error',
        agent: 'system',
        content: 'Dashboard API server offline. Please start the API server: python dashboard_api.py',
      }]);
      setIsSimulating(false);
      setActiveFault(null);
    }
  };

  // DAG helpers
  const getNodeStyle = (nodeId: string) => {
    if (!activeFault) return { bg: '#121214', border: '#222226', color: '#71717a' };
    const isRoot =
      (activeFault === 'cpu' && nodeId === 'cpu') ||
      (activeFault === 'memory' && nodeId === 'mem') ||
      (activeFault === 'network' && nodeId === 'network');
    const isAffected = nodeId === 'brain' || nodeId === 'swarm' || nodeId === 'exit';

    if (isRoot) return { bg: 'rgba(239,68,68,0.03)', border: '#dc2626', color: '#f87171' };
    if (isAffected) return { bg: 'rgba(251,191,36,0.02)', border: '#d97706', color: '#fbbf24' };
    return { bg: '#09090b', border: '#1c1c1f', color: '#3f3f46' };
  };

  const getEdgeStyle = (from: string, to: string) => {
    if (!activeFault) return { stroke: '#1c1c1f', strokeWidth: 1.5, strokeDasharray: 'none', animation: 'none' };
    const isPath =
      (activeFault === 'cpu' && (from === 'cpu' || (from === 'exit' && to === 'brain') || (from === 'brain' && to === 'swarm'))) ||
      (activeFault === 'memory' && (from === 'mem' || (from === 'exit' && to === 'brain') || (from === 'brain' && to === 'swarm'))) ||
      (activeFault === 'network' && (from === 'network' || (from === 'exit' && to === 'brain') || (from === 'brain' && to === 'swarm')));
    if (isPath) return { stroke: '#fbbf24', strokeWidth: 1.8, strokeDasharray: '4 3', animation: 'dash 1s linear infinite' };
    return { stroke: '#111113', strokeWidth: 1, strokeDasharray: 'none', animation: 'none' };
  };

  return (
    <div className="min-h-screen flex flex-col bg-zinc-950 text-zinc-100">
      {/* ---- Header ---- */}
      <header className="sticky top-0 z-50 border-b border-[#222226] bg-zinc-950/80 backdrop-blur-md">
        <div className="max-w-[1600px] mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="font-bold text-sm tracking-tight text-zinc-100 uppercase font-mono">eBPF-Swarm</span>
            <span className="text-zinc-500 text-[10px] font-mono border-l border-zinc-800 pl-2">K8s Operator Console</span>
          </div>

          <div className="flex items-center gap-4 text-[10px] font-mono">
            <div className="flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
              <span className="text-zinc-400">Probes: <strong className="text-zinc-200">Active</strong></span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${apiConnected ? 'bg-emerald-500' : 'bg-zinc-600'}`} />
              <span className="text-zinc-400">API: <strong className={apiConnected ? 'text-emerald-400 font-semibold' : 'text-zinc-500'}>{apiConnected ? 'Connected' : 'Offline'}</strong></span>
            </div>
            {aiConfig && (
              <div className="flex items-center gap-1.5 border border-[#222226] px-2 py-0.5 rounded bg-zinc-900/40 text-zinc-400">
                <span>Model: </span>
                <strong className="text-indigo-400 font-semibold">{aiConfig.provider === 'nvidia' ? 'NVIDIA NIM' : aiConfig.provider === 'openai' ? 'OpenAI' : 'Offline'}</strong>
              </div>
            )}
          </div>
        </div>
      </header>

      {/* ---- Main Grid ---- */}
      <main className="flex-1 max-w-[1600px] w-full mx-auto px-6 py-4 grid grid-cols-12 gap-4">

        {/* ==== Left Column: Controls + Metrics ==== */}
        <section className="col-span-12 lg:col-span-4 xl:col-span-3 flex flex-col gap-4">

          {/* Fault Injection Control Panel */}
          <div className="glass-card p-4">
            <h2 className="text-[10px] font-bold font-mono uppercase tracking-wider text-zinc-500 mb-3 flex items-center gap-1.5">
              <Zap className="w-3.5 h-3.5 text-zinc-400" />
              Fault Injection Console
            </h2>

            <div className="flex flex-col gap-2">
              {[
                { type: 'cpu', label: 'cpu-spike', param: '500m throttle', color: '#818cf8', desc: 'cgroup cpu.max constraint' },
                { type: 'memory', label: 'memory-leak', param: 'OOM trigger', color: '#60a5fa', desc: 'cgroup memory.max breach' },
                { type: 'network', label: 'network-partition', param: 'TCP drop 50%', color: '#fbbf24', desc: 'ebpf tc retransmit drops' },
              ].map(({ type, label, param, color, desc }) => (
                <button
                  key={type}
                  onClick={() => injectFault(type)}
                  disabled={isSimulating}
                  className={`chaos-btn w-full py-2.5 px-3 rounded text-left border transition-colors ${activeFault === type
                    ? 'border-red-950 bg-red-950/20 text-red-200'
                    : 'border-[#222226] bg-[#121214] text-zinc-300 hover:border-zinc-700 hover:bg-[#18181b] disabled:opacity-40 disabled:cursor-not-allowed'
                    }`}
                >
                  <div className="flex items-center justify-between text-xs font-mono mb-0.5">
                    <span className="font-bold flex items-center gap-1.5">
                      <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} />
                      {label}
                    </span>
                    <span className="text-[10px] text-zinc-500">[{param}]</span>
                  </div>
                  <span className="block text-[10px] text-zinc-600 font-mono pl-3">{desc}</span>
                </button>
              ))}
            </div>

            {isSimulating && (
              <div className="mt-3 p-2 border border-indigo-950 bg-indigo-950/10 rounded flex items-center gap-2 text-[10px] font-mono text-indigo-400">
                <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                <span>Orchestrator active. Running diagnostic loop...</span>
              </div>
            )}
          </div>

          {/* Telemetry Panel */}
          <div className="glass-card p-4">
            <h2 className="text-[10px] font-bold font-mono uppercase tracking-wider text-zinc-500 mb-4 flex items-center gap-1.5">
              <Activity className="w-3.5 h-3.5 text-zinc-400" />
              Resource Telemetry
            </h2>

            <div className="flex flex-col gap-3 mb-4">
              <MetricBar value={metrics.cpu} max={100} label="CPU UTILIZATION" unit="%" color="#818cf8" dangerThreshold={80} />
              <MetricBar value={metrics.memory} max={256} label="MEMORY UTILIZATION" unit="MiB" color="#60a5fa" dangerThreshold={200} />
              <MetricBar value={metrics.tcpRetransmits} max={100} label="TCP DROP RATE" unit="%" color="#fbbf24" dangerThreshold={10} />
            </div>

            <div className="grid grid-cols-2 gap-2 border-t border-[#222226] pt-3">
              <div className="p-2.5 rounded bg-zinc-950 border border-[#222226]">
                <span className="text-[9px] text-zinc-500 uppercase font-mono block">Self-Heals</span>
                <span className="text-base font-bold font-mono text-zinc-100">{metrics.restarts}</span>
              </div>
              <div className="p-2.5 rounded bg-zinc-950 border border-[#222226]">
                <span className="text-[9px] text-zinc-500 uppercase font-mono block">Last Recovery</span>
                <div className="flex items-baseline gap-1 font-mono text-zinc-100">
                  <span className="text-base font-bold">{metrics.healTime > 0 ? `${metrics.healTime}` : '—'}</span>
                  {metrics.healTime > 0 && <span className="text-[9px] text-zinc-500">sec</span>}
                </div>
              </div>
            </div>

            {/* Active Pod */}
            <div className="mt-3 p-2 rounded bg-zinc-950/60 border border-[#222226] flex items-center justify-between text-[10px] font-mono">
              <div className="flex items-center gap-2 min-w-0">
                <Server className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
                <span className="text-zinc-400 truncate">{metrics.podName}</span>
              </div>
              <span className="badge badge-success text-[8px] shrink-0 font-mono">RUNNING</span>
            </div>
          </div>
        </section>

        {/* ==== Right Column: DAG + Diagnostic Log ==== */}
        <section className="col-span-12 lg:col-span-8 xl:col-span-9 flex flex-col gap-4">

          {/* Causal DAG */}
          <div className="glass-card p-4 relative" style={{ minHeight: 280 }}>
            <div className="flex items-center justify-between mb-3 border-b border-[#222226] pb-2">
              <div>
                <h2 className="text-[10px] font-bold font-mono uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
                  <Activity className="w-3.5 h-3.5 text-zinc-400" />
                  Causal Discovery Graph
                </h2>
                <p className="text-[9px] text-zinc-600 font-mono mt-0.5">eBPF probe dependency paths & root cause isolation paths</p>
              </div>
              <div className="flex items-center gap-3 text-[9px] font-mono uppercase text-zinc-500">
                <span className="flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-full bg-zinc-700" /> Normal</span>
                <span className="flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-full bg-red-600" /> Root Cause</span>
                <span className="flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-full bg-amber-500" /> Affected</span>
              </div>
            </div>

            <div className="w-full rounded border border-[#222226] bg-zinc-950 relative overflow-hidden" style={{ height: 200 }}>
              <svg className="absolute inset-0 w-full h-full pointer-events-none">
                <defs>
                  <marker id="arrow" viewBox="0 0 10 10" refX="22" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                    <path d="M 0 0 L 10 5 L 0 10 z" fill="#27272a" />
                  </marker>
                  <marker id="arrow-active" viewBox="0 0 10 10" refX="22" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                    <path d="M 0 0 L 10 5 L 0 10 z" fill="#fbbf24" />
                  </marker>
                </defs>

                <line x1={NODES.network.x} y1={NODES.network.y} x2={NODES.exit.x} y2={NODES.exit.y}
                  style={getEdgeStyle('network', 'exit')} markerEnd={activeFault === 'network' ? 'url(#arrow-active)' : 'url(#arrow)'} />
                <line x1={NODES.cpu.x} y1={NODES.cpu.y} x2={NODES.exit.x} y2={NODES.exit.y}
                  style={getEdgeStyle('cpu', 'exit')} markerEnd={activeFault === 'cpu' ? 'url(#arrow-active)' : 'url(#arrow)'} />
                <line x1={NODES.mem.x} y1={NODES.mem.y} x2={NODES.exit.x} y2={NODES.exit.y}
                  style={getEdgeStyle('mem', 'exit')} markerEnd={activeFault === 'memory' ? 'url(#arrow-active)' : 'url(#arrow)'} />
                <line x1={NODES.exit.x} y1={NODES.exit.y} x2={NODES.brain.x} y2={NODES.brain.y}
                  style={getEdgeStyle('exit', 'brain')} markerEnd={activeFault ? 'url(#arrow-active)' : 'url(#arrow)'} />
                <line x1={NODES.brain.x} y1={NODES.brain.y} x2={NODES.swarm.x} y2={NODES.swarm.y}
                  style={getEdgeStyle('brain', 'swarm')} markerEnd={activeFault ? 'url(#arrow-active)' : 'url(#arrow)'} />
              </svg>

              <div className="absolute inset-0">
                {Object.values(NODES).map((node) => {
                  const Icon = node.icon;
                  const style = getNodeStyle(node.id);
                  return (
                    <div
                      key={node.id}
                      className="absolute p-2 rounded text-left flex flex-col gap-0.5 transition-colors duration-300 z-10"
                      style={{
                        left: `${node.x - 75}px`,
                        top: `${node.y - 30}px`,
                        width: 150,
                        background: style.bg,
                        border: `1px solid ${style.border}`,
                        color: style.color,
                      }}
                    >
                      <div className="flex items-center gap-1.5">
                        <Icon className="w-3.5 h-3.5 shrink-0" />
                        <span className="text-[10px] font-bold font-mono tracking-tight truncate">{node.label}</span>
                      </div>
                      <span className="text-[8px] font-mono leading-tight opacity-50">{node.desc}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {/* Autonomic Orchestration Log & Trace */}
          <div className="glass-card p-4 flex flex-col" style={{ height: 'calc(100vh - 400px)', minHeight: 400 }}>
            <div className="flex items-center justify-between mb-3 border-b border-[#222226] pb-2">
              <div>
                <h2 className="text-[10px] font-bold font-mono uppercase tracking-wider text-zinc-500">
                  Orchestration Log & Diagnostic Trace
                </h2>
                <p className="text-[9px] text-zinc-600 font-mono mt-0.5">Real-time trace logs of autonomic SRE execution loop</p>
              </div>
              <div className="flex items-center gap-2">
                {isSimulating && (
                  <div className="flex items-center gap-1.5 px-2 py-0.5 rounded bg-red-950/20 border border-red-900/30 text-red-400 text-[9px] font-bold font-mono">
                    <span className="w-1 h-1 rounded-full bg-red-500 animate-pulse" />
                    LIVE RUNNING
                  </div>
                )}
                <span className="text-[10px] text-zinc-500 font-mono">{events.length} lines</span>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto rounded p-3 flex flex-col bg-zinc-950 border border-[#222226]">
              {events.length === 0 ? (
                <div className="flex-1 flex flex-col items-center justify-center text-center py-12">
                  <div className="w-10 h-10 rounded border border-[#222226] bg-zinc-900/20 flex items-center justify-center mb-3">
                    <Terminal className="w-5 h-5 text-zinc-700" />
                  </div>
                  <span className="text-xs text-zinc-500 font-semibold font-mono">Trace stream idle</span>
                  <span className="text-[10px] text-zinc-700 font-mono mt-0.5">Trigger a fault script to capture eBPF and SRE swarm output.</span>
                </div>
              ) : (
                events.map((evt) => <EventMessage key={evt.id} event={evt} />)
              )}
              <div ref={chatEndRef} />
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

export default App;
