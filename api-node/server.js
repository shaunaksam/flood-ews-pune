const express = require("express");
const jwt = require("jsonwebtoken");
const bcrypt = require("bcryptjs");
const app = express();
app.use(express.json());

const PORT = 4000;
const RISK_API = "http://127.0.0.1:8000";
const JWT_SECRET = "dev-only-secret-change-before-real-deployment";

// Hardcoded demo users for now 
// Password is "demo123" for both
const DEMO_USERS = {
  citizen: { password: "$2b$10$CqmpnpyTgGEJjnOlzSGTu.zBr2gCF15rFT1oCBOUiBxsBe0YHm1fu", role: "citizen" },
  officer: { password: "$2b$10$CqmpnpyTgGEJjnOlzSGTu.zBr2gCF15rFT1oCBOUiBxsBe0YHm1fu", role: "officer" },
};

app.get("/health", (req, res) => res.json({ status: "ok", service: "node-backend" }));

app.post("/api/login", async (req, res) => {
  const { username, password } = req.body;
  const user = DEMO_USERS[username];
  if (!user || !(await bcrypt.compare(password, user.password))) {
    return res.status(401).json({ error: "Invalid username or password" });
  }
  const token = jwt.sign({ username, role: user.role }, JWT_SECRET, { expiresIn: "2h" });
  res.json({ token, role: user.role });
});

function requireAuth(req, res, next) {
  const authHeader = req.headers.authorization;
  if (!authHeader) return res.status(401).json({ error: "No token provided" });
  try {
    req.user = jwt.verify(authHeader.replace("Bearer ", ""), JWT_SECRET);
    next();
  } catch {
    res.status(401).json({ error: "Invalid or expired token" });
  }
}

app.get("/api/risk/city", requireAuth, async (req, res) => {
  const { current_cusecs = 15000, rain_24h_mm = 0 } = req.query;
  const r = await fetch(`${RISK_API}/risk/city?current_cusecs=${current_cusecs}&rain_24h_mm=${rain_24h_mm}`);
  const data = await r.json();
  res.json({ ...data, requested_by: req.user.username, role: req.user.role });
});

app.listen(PORT, () => console.log(`Node backend running on http://127.0.0.1:${PORT}`));