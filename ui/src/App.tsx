import { TraceProvider } from './context/TraceProvider';
import { useTraceContext } from './context/TraceContext';
import { WelcomeView } from './components/welcome/WelcomeView';
import { UploadView } from './components/upload/UploadView';
import { DashboardView } from './components/dashboard/DashboardView';
import { InspectorView } from './components/inspector/InspectorView';
import { BuilderView } from './components/builder/BuilderView';
import { LiveStreamingView } from './components/streaming/LiveStreamingView';

function AppContent() {
  const { state } = useTraceContext();

  return (
    <>
      {state.currentView === 'welcome' && <WelcomeView />}
      {state.currentView === 'upload' && <UploadView />}
      {state.currentView === 'dashboard' && <DashboardView />}
      {state.currentView === 'inspector' && <InspectorView />}
      {state.currentView === 'builder' && <BuilderView />}
      {state.currentView === 'streaming' && <LiveStreamingView />}
      {state.currentView === 'comparison' && (
        <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-secondary)' }}>
          Comparison view coming soon...
        </div>
      )}
    </>
  );
}

function App() {
  return (
    <TraceProvider>
      <AppContent />
    </TraceProvider>
  );
}

export default App;
