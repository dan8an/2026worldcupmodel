import { NavLink, Route, Routes } from "react-router-dom";
import { Dashboard } from "./pages/Dashboard";
import { MatchDetail } from "./pages/MatchDetail";
import { Matches } from "./pages/Matches";
import { Methodology } from "./pages/Methodology";
import { Simulator } from "./pages/Simulator";
import { TeamDetail } from "./pages/TeamDetail";
import { Teams } from "./pages/Teams";

const links = [
  ["/", "Dashboard"],
  ["/matches", "Matches"],
  ["/teams", "Teams"],
  ["/simulator", "Simulator"],
  ["/model-explainer", "Model"],
];

export default function App() {
  return (
    <div className="app-shell">
      <header className="site-header">
        <NavLink to="/" className="brand">
          <span className="brand-mark">26</span>
          <span>
            <strong>World Cup Forecast</strong>
            <small>Prediction and analytics</small>
          </span>
        </NavLink>
        <nav>
          {links.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              {label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/matches" element={<Matches />} />
          <Route path="/match/:id" element={<MatchDetail />} />
          <Route path="/teams" element={<Teams />} />
          <Route path="/teams/:id" element={<TeamDetail />} />
          <Route path="/simulator" element={<Simulator />} />
          <Route path="/model-explainer" element={<Methodology />} />
        </Routes>
      </main>
      <footer>
        Educational model probabilities, not betting advice. Context-adjusted
        model pending chronological calibration.
      </footer>
    </div>
  );
}
