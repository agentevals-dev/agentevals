// API envelope
export interface StandardResponse<T> {
  data: T;
  error: string | null;
}

// Core trace data structures
export interface Log {
  timestamp: number;
  fields: Record<string, any>;
}

export interface Span {
  traceId: string;
  spanId: string;
  parentSpanId: string | null;
  operationName: string;
  startTime: number; // microseconds
  duration: number; // microseconds
  tags: Record<string, any>;
  logs: Log[];
  children: Span[];
}

export interface Trace {
  traceId: string;
  rootSpans: Span[];
  allSpans: Span[];
}

// ADK Invocation structures
export interface Content {
  role: string;
  parts: Part[];
}

export interface Part {
  text?: string;
  functionCall?: FunctionCall;
  functionResponse?: FunctionResponse;
}

export interface FunctionCall {
  name: string;
  args: Record<string, any>;
  id?: string;
}

export interface FunctionResponse {
  name: string;
  response: Record<string, any>;
  id?: string;
}

export interface ToolCall {
  name: string;
  args: Record<string, any>;
  id?: string;
}

export interface ToolResponse {
  name: string;
  response: Record<string, any>;
  id?: string;
}

export interface IntermediateData {
  toolUses: ToolCall[];
  toolResponses: ToolResponse[];
}

export interface Invocation {
  invocationId: string;
  userContent: Content;
  finalResponse: Content;
  intermediateData: IntermediateData;
  creationTimestamp?: number;
}

// Trace conversion API response types
export interface TraceConversionMetadata {
  agentName?: string;
  model?: string;
  startTime?: number;
  userInputPreview?: string;
  finalOutputPreview?: string;
  sessionName?: string;
}

export interface TraceConversionEntry {
  traceId: string;
  invocations: Invocation[];
  warnings: string[];
  metadata: TraceConversionMetadata;
}

export interface ConvertTracesResponse {
  traces: TraceConversionEntry[];
}

// Evaluation results
export type EvalStatus = 'PASSED' | 'FAILED' | 'NOT_EVALUATED' | 'ERROR';

export interface ToolCallComparison {
  name: string;
  args: Record<string, any>;
}

export interface TrajectoryComparison {
  invocationId: string | null;
  expected: ToolCallComparison[];
  actual: ToolCallComparison[];
  matched: boolean;
}

export interface MetricDetails {
  comparisons?: TrajectoryComparison[];
}

export interface MetricResult {
  metricName: string;
  score: number | null;
  evalStatus: EvalStatus;
  perInvocationScores: (number | null)[];
  error: string | null;
  details?: MetricDetails | null;
}

export interface PerformanceMetrics {
  latency: {
    overall: { p50: number; p95: number; p99: number };
    llmCalls: { p50: number; p95: number; p99: number };
    toolExecutions: { p50: number; p95: number; p99: number };
  };
  tokens: {
    totalPrompt: number;
    totalOutput: number;
    total: number;
    perLlmCall: { p50: number; p95: number; p99: number };
    cacheCreationTokens?: number;
    cacheReadTokens?: number;
  };
}

export interface TraceResult {
  traceId: string;
  sessionId?: string;
  numInvocations: number;
  metricResults: MetricResult[];
  conversionWarnings: string[];
  performanceMetrics?: PerformanceMetrics;
  agentName?: string;
  agentId?: string;
  model?: string;
  responseModel?: string;
  provider?: string;
  startTime?: number;
  userInputPreview?: string;
  finalOutputPreview?: string;
}

export interface RunResultPerformanceMetrics {
  tokens: {
    totalPrompt: number;
    totalOutput: number;
    total: number;
    avgPerTrace: {
      prompt: number;
      output: number;
    };
  };
  traceCount: number;
}

export interface RunResult {
  traceResults: TraceResult[];
  errors: string[];
  performanceMetrics?: RunResultPerformanceMetrics;
}

// Table-specific types
export type TraceRowStatus = 'pending' | 'loading' | 'complete' | 'error';

export interface TraceTableRow {
  traceId: string;
  sessionId?: string;
  status: TraceRowStatus;
  agentName?: string;
  agentId?: string;
  startTime?: number;
  model?: string;
  responseModel?: string;
  provider?: string;
  userInputPreview?: string;
  finalOutputPreview?: string;
  metricResults: Map<string, MetricResult>;
  numInvocations?: number;
  invocations?: Invocation[];
  conversionWarnings: string[];
  error?: string;
  performanceMetrics?: PerformanceMetrics;
  annotation?: Annotation;
}

// Evaluation API config
export interface EvalConfig {
  evaluators: Array<{
    type: 'builtin';
    name: string;
    judgeModel?: string;
    threshold?: number;
    trajectoryMatchType?: string;
  }>;
}

// EvalSet types
export interface EvalSetMetadata {
  eval_set_id: string;
  name: string;
  description: string;
}

export interface EvalCase {
  eval_id: string;
  conversation: Invocation[];
}

export interface EvalSet {
  eval_set_id: string;
  name: string;
  description: string;
  eval_cases: EvalCase[];
}

// View types
export type ViewType = 'welcome' | 'upload' | 'dashboard' | 'inspector' | 'comparison' | 'builder' | 'streaming' | 'annotation-queue' | 'runs';

// Persisted run history (durable storage, GET /api/runs)
export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface ResultCounts {
  passed: number;
  failed: number;
  errored: number;
  skipped: number;
}

// One entry per evaluator in summary.per_metric. avg_score is null when no
// invocation of that evaluator produced a numeric score (e.g. it only errored).
export interface PerMetricSummary extends ResultCounts {
  avg_score: number | null;
}

// summary is a free-form dict on the backend, so its keys stay snake_case on
// the wire (the camelCase alias generator only renames declared model fields).
export interface RunSummary {
  trace_count: number;
  result_counts: ResultCounts;
  per_metric?: Record<string, PerMetricSummary>;
  agents?: string[];
  errors?: string[];
  performance_metrics?: { models?: string[] } & Record<string, unknown>;
}

// spec.evalSet/evalConfig are stored as raw dicts, so their inner keys keep
// the original snake_case (e.g. ADK eval_set_id) rather than being camelCased.
export interface RunEvaluatorConfig {
  name?: string;
  type?: string;
  threshold?: number;
  judge_model?: string;
  trajectory_match_type?: string;
}

export interface RunSpecSummary {
  approach?: string;
  evalSet?: { eval_set_id?: string; name?: string; eval_cases?: unknown[] } | null;
  evalConfig?: { evaluators?: RunEvaluatorConfig[] } & Record<string, unknown>;
  target?: { kind?: string; trace_count?: number; trace_files?: string[] } & Record<string, unknown>;
}

// One persisted Result row (GET /api/runs/{id}/results). Top-level fields are
// camelCased by the API; the free-form `details` keeps snake_case inner keys.
export type ResultStatus2 = 'passed' | 'failed' | 'errored' | 'skipped';

export interface PersistedComparison {
  invocation_id?: string | null;
  matched?: boolean;
  expected?: ToolCallComparison[];
  actual?: ToolCallComparison[];
}

export interface ResultDetails {
  comparisons?: PersistedComparison[];
  [key: string]: unknown;
}

export interface RunResultRow {
  resultId: string;
  runId: string;
  evalSetItemId: string;
  evalSetItemName: string;
  evaluatorName: string;
  evaluatorType: string;
  status: ResultStatus2;
  score: number | null;
  perInvocationScores: (number | null)[];
  traceId?: string | null;
  spanId?: string | null;
  details?: ResultDetails;
  errorText?: string | null;
  tokensUsed?: Record<string, unknown> | null;
  latencyMs?: number | null;
  createdAt: string;
}

export interface Run {
  runId: string;
  status: RunStatus;
  spec: RunSpecSummary;
  attempt?: number;
  workerId?: string | null;
  error?: string | null;
  summary?: RunSummary | null;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  cancelRequested?: boolean;
}

// Metric metadata type
export interface MetricMetadata {
  name: string;
  category: string;
  requiresEvalSet?: boolean;
  requiresLLM?: boolean;
  requiresGCP?: boolean;
  requiresRubrics?: boolean;
  working?: boolean;
  description: string;
}

// Available metrics (from ADK) - Default fallback
// Note: In production, these should be loaded from the API /metrics endpoint
export const AVAILABLE_METRICS: MetricMetadata[] = [
  // Trajectory metrics
  {
    name: 'tool_trajectory_avg_score',
    category: 'trajectory',
    requiresEvalSet: true,
    requiresLLM: false,
    requiresGCP: false,
    requiresRubrics: false,
    working: true,
    description: 'Compare tool call sequences against expected trajectory'
  },
  // Response metrics
  {
    name: 'response_match_score',
    category: 'response',
    requiresEvalSet: true,
    requiresLLM: false,
    requiresGCP: false,
    requiresRubrics: false,
    working: true,
    description: 'Text similarity between actual and expected responses using ROUGE-1'
  },
  {
    name: 'response_evaluation_score',
    category: 'response',
    requiresEvalSet: true,
    requiresLLM: false,
    requiresGCP: true,
    requiresRubrics: false,
    working: true,
    description: 'Semantic evaluation of response quality using Vertex AI'
  },
  {
    name: 'final_response_match_v2',
    category: 'response',
    requiresEvalSet: true,
    requiresLLM: true,
    requiresGCP: false,
    requiresRubrics: false,
    working: true,
    description: 'LLM-based comparison of final responses'
  },
  // Quality metrics
  {
    name: 'rubric_based_final_response_quality_v1',
    category: 'quality',
    requiresEvalSet: false,
    requiresLLM: true,
    requiresGCP: false,
    requiresRubrics: true,
    working: false,
    description: 'Rubric-based quality assessment of responses (requires rubrics config)'
  },
  {
    name: 'rubric_based_tool_use_quality_v1',
    category: 'quality',
    requiresEvalSet: false,
    requiresLLM: true,
    requiresGCP: false,
    requiresRubrics: true,
    working: false,
    description: 'Rubric-based assessment of tool usage quality (requires rubrics config)'
  },
  // Safety metrics
  {
    name: 'hallucinations_v1',
    category: 'safety',
    requiresEvalSet: false,
    requiresLLM: true,
    requiresGCP: false,
    requiresRubrics: false,
    working: true,
    description: 'Detect hallucinated information in responses'
  },
  {
    name: 'safety_v1',
    category: 'safety',
    requiresEvalSet: false,
    requiresLLM: false,
    requiresGCP: true,
    requiresRubrics: false,
    working: true,
    description: 'Safety and security assessment using Vertex AI'
  },
  // Multi-turn metrics (Vertex AI Gen AI Eval SDK)
  {
    name: 'multi_turn_task_success_v1',
    category: 'multi-turn',
    requiresEvalSet: false,
    requiresLLM: false,
    requiresGCP: true,
    requiresRubrics: false,
    working: true,
    description: 'Evaluates if the agent achieved the goal(s) of the multi-turn conversation (Vertex AI)'
  },
  {
    name: 'multi_turn_trajectory_quality_v1',
    category: 'multi-turn',
    requiresEvalSet: false,
    requiresLLM: false,
    requiresGCP: true,
    requiresRubrics: false,
    working: true,
    description: 'Evaluates the overall trajectory the agent took across the conversation (Vertex AI)'
  },
  {
    name: 'multi_turn_tool_use_quality_v1',
    category: 'multi-turn',
    requiresEvalSet: false,
    requiresLLM: false,
    requiresGCP: true,
    requiresRubrics: false,
    working: true,
    description: 'Evaluates function calls made during a multi-turn conversation (Vertex AI)'
  }
];

// Streaming / Live session types
export interface ConversationElement {
  type: 'user_input' | 'tool_call' | 'tool_result' | 'agent_response';
  timestamp: number;
  invocationId: string;
  data: any;
}

export interface StreamingInvocation {
  invocationId: string;
  userText: string;
  agentText: string;
  toolCalls: Array<{ name: string; args: any; id?: string }>;
  toolResponses?: Array<{ name: string; response: Record<string, any>; id?: string }>;
}

export interface LiveSession {
  sessionId: string;
  traceId: string;
  evalSetId: string | null;
  spans: any[];
  status: 'active' | 'complete';
  metadata: Record<string, any>;
  invocations?: StreamingInvocation[];
  liveElements: ConversationElement[];
  liveStats: {
    totalInputTokens: number;
    totalOutputTokens: number;
    model?: string;
  };
  startedAt: string;
}

// Inspector-specific types
export type ExtractionType = 'user_input' | 'tool_use' | 'tool_response' | 'final_response';

export interface ExtractionInfo {
  type: ExtractionType;
  invocationId: string;
  spanId: string;
  tagPath: string;
  jsonPath: string;
  lineRange: { start: number; end: number };
}

export interface SpanReference {
  spanId: string;
  extractionType: ExtractionType;
  tagKey: string;
}

export interface TraceMapping {
  spanToData: Map<string, ExtractionInfo[]>;
  dataToSpan: Map<string, SpanReference>;
  jsonLineRanges: Map<string, { start: number; end: number }>;
}

export interface InspectorUIState {
  selectedInvocationId: string | null;
}

// Trace editor types
export type TraceFileFormat = 'jaeger' | 'otlp-jsonl';

export interface ToolMessageEditLocation {
  spanId: string;
  attrKey: string;
}

export interface SpanLocationRef {
  traceIndex?: number;
  spanIndex?: number;
  lineIndex?: number;
}

export interface ParsedTraceFile {
  format: TraceFileFormat;
  fileName: string;
  rawData: any;
  spanIndex: Map<string, SpanLocationRef>;
}

export interface SpanEditMapping {
  invocationId: string;
  format: 'adk' | 'genai';
  userInputSpanId: string;
  finalResponseSpanId: string;
  toolSpanIds: string[];
  toolMessageLocations?: ToolMessageEditLocation[];
  userInputAttrKey: string;
  finalResponseAttrKey: string;
}

// Annotation queue types
export type FirstPassLabel = 'looks_correct' | 'doesnt_look_correct';

export interface Annotation {
  firstPass: FirstPassLabel;
  comment: string;
  annotatedAt: string;
}

export interface AnnotationQueueItem {
  sessionId: string;
  traceId: string;
  agentName?: string;
  startTime?: string;
  model?: string;
  totalTokens?: number;
  annotation?: Annotation;
  invocations?: StreamingInvocation[];
  liveElements?: ConversationElement[];
  liveStats?: { totalInputTokens: number; totalOutputTokens: number; model?: string };
  metadata?: Record<string, any>;
}

export interface AnnotationQueue {
  id: string;
  name: string;
  items: AnnotationQueueItem[];
  createdAt: string;
}
