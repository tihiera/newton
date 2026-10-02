import { AppConnectionBoundary } from "./app/AppConnectionBoundary";
import { NavigationProvider } from "./app/navigation";
import { NewtonShell } from "./app/NewtonShell";
import { EventsProvider } from "./hooks/useEvents";

export default function App() {
  return (
    <AppConnectionBoundary>
      <EventsProvider>
        <NavigationProvider>
          <NewtonShell />
        </NavigationProvider>
      </EventsProvider>
    </AppConnectionBoundary>
  );
}
