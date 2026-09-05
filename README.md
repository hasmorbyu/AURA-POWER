# AURA — AI-Powered Grid Resilience

**AURA (Autonomous Utility Resilience Assistant)** is an AI-powered grid intelligence platform designed to help Delhi's power grid **predict demand, detect stress, and automatically recommend safer power-flow decisions** before failures become outages.

## The Problem

Delhi's electricity demand can spike rapidly due to heat, events, and unexpected grid failures. Operators need to answer:

> **"What is going to happen to the grid, and what should we do before it becomes unstable?"**

Traditional monitoring shows what is happening now. AURA focuses on **what happens next**.

## What AURA Does

**1. Predict**

* Forecasts electricity demand using a LightGBM model.
* Incorporates weather, time, holidays, events, and other demand drivers.

**2. Simulate**

* Models the transmission network as a live graph.
* Simulates demand spikes, temperature escalation, and transmission-line failures.

**3. Optimize**

* Detects overloaded or high-risk network paths.
* Recommends power-flow rerouting and load balancing.
* Uses emergency actions only as a last resort.

**4. Explain**

* Shows *why* the grid is under stress.
* Visualizes affected substations, transmission lines, load, and recommended actions.

## Architecture

```text
Weather + Demand + Events
          │
          ▼
   AI Demand Forecast
       (LightGBM)
          │
          ▼
   Grid Digital Twin
   ┌─────────────────┐
   │ Substations     │
   │ Transmission    │
   │ Generation      │
   │ Loads           │
   └─────────────────┘
          │
          ▼
  Grid Optimization Engine
          │
     ┌────┴────┐
     ▼         ▼
 Rebalance   Stress Test
     │         │
     └────┬────┘
          ▼
   Operator Dashboard
```

## Key Features

* AI-based demand forecasting
* Interactive Delhi transmission-grid visualization
* Real-time grid stress simulation
* Transmission-line failure simulation
* Automated power-flow rerouting
* Load balancing recommendations
* Explainable grid-risk indicators
* Judge-friendly live stress-test mode

## Tech Stack

| Layer                | Technology                                          |
| -------------------- | --------------------------------------------------- |
| Forecasting          | LightGBM, Python                                    |
| Grid Optimization    | Python, Network/optimization algorithms             |
| Backend              | Python / API services                               |
| Frontend             | Web-based interactive dashboard                     |
| Maps & Visualization | Interactive network visualization                   |
| Data                 | Power demand, weather, events & grid infrastructure |

## Demo Scenarios

### Proactive Rebalancing

AURA forecasts an upcoming demand spike and identifies stressed parts of the grid **before overload occurs**, then recommends corrective power-flow changes.

### Grid Stress Test

Increase demand, raise temperature, or fail a transmission line live.

AURA continuously recalculates the grid state and attempts to:

**Reroute → Rebalance → Protect**

without resetting the simulation state.

## Goal

> **Turn the power grid from reactive infrastructure into a predictive, self-optimizing system.**

AURA is designed as a decision-support layer for grid operators — helping them see problems earlier and act before they become outages.
