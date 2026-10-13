interface ConsoleEntry {
  timestamp: string;
  level: 'log' | 'warn' | 'error';
  args: string[];
}

const MAX_ENTRIES = 500;
const buffer: ConsoleEntry[] = [];

let installed = false;

export function installConsoleCapture(): void {
  if (installed) return;
  installed = true;

  const originalLog = console.log;
  const originalWarn = console.warn;
  const originalError = console.error;

  function capture(
    level: ConsoleEntry['level'],
    original: (...args: unknown[]) => void,
  ) {
    return (...args: unknown[]) => {
      original.apply(console, args);
      const entry: ConsoleEntry = {
        timestamp: new Date().toISOString(),
        level,
        args: args.map((a) => {
          try {
            return typeof a === 'string' ? a : JSON.stringify(a);
          } catch {
            return String(a);
          }
        }),
      };
      buffer.push(entry);
      if (buffer.length > MAX_ENTRIES) {
        buffer.shift();
      }
    };
  }

  console.log = capture('log', originalLog);
  console.warn = capture('warn', originalWarn);
  console.error = capture('error', originalError);
}

export function getConsoleLogs(): ConsoleEntry[] {
  return [...buffer];
}
