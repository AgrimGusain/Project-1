import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Camera from "./pages/Camera";
import Dashboard from "./pages/Dashboard";
import Drive from "./pages/Drive";
import Mission from "./pages/Mission";
import Missions, { MissionDetail } from "./pages/Missions";
import PlanGps from "./pages/PlanGps";
import PlanGrid from "./pages/PlanGrid";
import Sensors from "./pages/Sensors";
import Settings from "./pages/Settings";
import System from "./pages/System";
import { TelemetryProvider } from "./state/telemetry";

export default function App() {
  return (
    <TelemetryProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Dashboard />} />
            <Route path="drive" element={<Drive />} />
            <Route path="plan/grid" element={<PlanGrid />} />
            <Route path="plan/gps" element={<PlanGps />} />
            <Route path="mission" element={<Mission />} />
            <Route path="missions" element={<Missions />} />
            <Route path="missions/:id" element={<MissionDetail />} />
            <Route path="sensors" element={<Sensors />} />
            <Route path="camera" element={<Camera />} />
            <Route path="system" element={<System />} />
            <Route path="settings" element={<Settings />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </TelemetryProvider>
  );
}
