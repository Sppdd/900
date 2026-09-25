# Hackathon Requirements

Every project built in this repository must fit at least one of these tracks.

## Coding and Agentic Engineering Track
Build coding agents and developer tools: agents that write, run, and test code in **Token Factory Sandboxes**.

## Best Apps and Agents Track
Build any app or agent someone would actually use, from a productivity tool or copilot to a workflow that runs itself.
- Power it with **Nemotron models on Nebius through Token Factory**.
- **Nemotron 3 Ultra** for serious reasoning; **Nano / Super** for fast, everyday calls (keeps the app responsive and credits stretched).
- Encouraged (not required): deploy with **Nebius Serverless Endpoints**; use **Nebius Serverless Jobs** for background processing and async workflows.

## Personal AI Track
Build an always-on, private assistant that works for you while keeping your data under your control.
- Persistent memory, reusable skills, access to tools/information you choose, ability to carry out tasks across daily workflows.
- Use **at least one NVIDIA open source model**.
- Use tools such as **NVIDIA NemoClaw, OpenShell, Hermes Agent, Nebius Serverless** to assemble, secure, and run the system.

## Physical AI Track
Build embodied and edge agents that sense and act in the real world: robotics, IoT, on-device intelligence.
- Powered by **Nemotron, GR00T, Cosmos, Sonic** models, coordinated through an agent runtime.
- **Nebius Serverless Jobs**: simulations, synthetic data, robot policy evaluation, sensor data processing at scale.
- **Nebius Serverless Endpoints**: real-time inference when needed.
- Demo video must include **≥1 minute** of the physical hardware/robot operating — or, with no hardware, the key application modules in action.

---

# Official Rules — Key Points (Nebius x NVIDIA Global AI Hackathon, Devpost)

## Dates
- Submission: Aug 26, 2026 9:00 PT → **Oct 30, 2026 10:00 am PT**
- Judging: Dec 1–15, 2026 · Winners ~Jan 11, 2027

## Hard requirements (Stage One pass/fail)
- Working software that **runs on Nebius Token Factory or Nebius AI Cloud** = makes a runtime call to the Token Factory inference API, OR is deployed on Nebius AI Cloud (Serverless Jobs / Serverless Endpoints / DevPods).
- Uses **at least one NVIDIA open source model** (e.g. Nemotron).
- Genuinely fits one track (not a superficial rebrand). Must "reasonably apply" the featured APIs/SDKs.
- New, or significantly updated during the submission period (explain what changed).
- Authorized use of any third-party APIs/SDKs/data.

## Submission checklist
- [ ] URL to working demo / hosted app (free, unrestricted access through judging; include login creds if private)
- [ ] Text description of features
- [ ] **Public** GitHub/GitLab/Bitbucket repo with all source, assets, instructions
- [ ] **Open source LICENSE** (MIT / Apache-2.0 / MPL-2.0) visible in repo About section
- [ ] README with setup + run instructions, highlighting **how Nemotron/NVIDIA models are used**, **where Token Factory accelerated the workflow**, and other Nebius services used
- [ ] Demo video **< 3 min**, public on **YouTube**, shows project working; no unlicensed music/trademarks
- [ ] Track selection
- [ ] Feedback on Token Factory, AI Cloud, NVIDIA tools (eligible for Most Valuable Feedback)
- [ ] All materials in English

## Judging (Stage Two, equally weighted)
1. **Technological Implementation** — build quality; how effectively it uses Token Factory/AI Cloud + NVIDIA models (tie-breaker #1)
2. **Design** — complete, coherent product experience, not a PoC
3. **Potential Impact** — specific real problem, real audience, demonstrated solution
4. **Quality of the Idea** — creative, non-obvious use of Nebius + NVIDIA models; understanding of the problem space

## Prizes
- Overall: $20k / $10k / $6k · Track winners: NVIDIA Jetson Orin Nano each
- Bonus: **Best Use of Tavily $3,000** (functional runtime call to Tavily API) · City winners $500 · Most Valuable Feedback $100 + swag
- A project can win 1 Overall OR 1 Track award, **plus** 1 Bonus award.
- Nebius Builder Program gives credits for Token Factory, Tavily, Nebius Academy.
