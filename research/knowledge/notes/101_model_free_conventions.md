# Model-free Conventions in Multi-Agent Reinforcement Learning with Heterogeneous Preferences

**Authors**: Raphael Köster, Kevin R. McKee, Richard Everett, Laura Weidinger, William S. Isaac, Edward Hughes, Edgar A. Duéñez-Guzmán, Thore Graepel, Matthew Botvinick, Joel Z. Leibo
**Date**: 2020-10
**Topic**: Multi-Agent Reinforcement Learning (MARL), Conventions, Coordination, Heterogeneous Preferences, DeepMind

## Summary

The paper investigates how **Conventions** (shared behavioral rules) emerge in populations of **Model-Free (MF)** Reinforcement Learning agents with **Heterogeneous Preferences** (diﬀerent tastes).

- **The Problem**: In a "Large World" with incomplete information, "Rational" (Model-Based) coordination is impossible (too many equilibrium states to compute). Evolution is too slow.
- **The Solution**: **Habit Learning (MF RL)** allows agents to stumble upon and stabilize conventions through "Learning by Doing" and joint exploration.
- **The Game (Allelopathic Harvest)**:
  - Gridworld where agents plant/harvest berries.
  - $K$ berry colors. Agents prefer specific colors (Heterogeneity).
  - **Monoculture Bonus**: Berries ripen faster if there are more of the _same_ color (Coordination Incentive).
  - **Conflict**: Red-preferring agents want a Red Monoculture; Blue-preferring agents want Blue.
- **Key Findings**:
  - **Emergence**: MF agents successfully establish conventions (e.g., everyone plants Red, even Blue-lovers, to maximize total yield).
  - **Start-Up Problem**: Small groups struggle to initiate a convention. A "Critical Mass" is needed.
  - **Free-Rider Problem**: Once a convention is established, agents stop planting and just harvest, leading to sub-optimal yields (Public Goods dilemma).
  - **Salience**: Visual salience (e.g., brighter berries) can break symmetry and act as a "Focal Point" (Schelling Point) for convention selection.

## Key Concepts

1.  **Conventions as Habits**:
    - Conventions aren't always rational contracts. They are often "frozen habits" sustained by MF value functions.
    - Agents resist "Rapid Revaluation" (shifting to a better equilibrium immediately) because their Value Function $Q(s,a)$ has inertia. This matches human social inertia.
2.  **Conflictual Coordination**:
    - Situations where _any_ coordination is better than none, but agents disagree on _which_ coordination point is best.
    - The "Start-Up" phase is the hardest: "Why should I plant Red if no one else is?"

## Implications for Our Platform

- **Agent Simulation**:
  - **Modeling Order Flow**: When simulating a market with multiple RL agents (e.g., HFTs vs. Executing Brokers), expect **Inertia**. Agents won't instantly switch strategies just because market regime changes; they will need to "unlearn" the old convention.
  - **Initialization**: To bootstrap a healthy market simulation, initialize with a "Critical Mass" of liquidity providers, or the market will die (Start-Up Problem).
- **RL Training**:
  - **Curriculum**: To train a robust HFT agent, we should expose it to different "Conventions" (e.g., determining who is the aggressive liquidity taker).
  - **Color/Salience**: In our state representation, "Salient" features (e.g., large volume spikes) will naturally become Focal Points for coordination. Ensure these features are robust signals.

## Tags

#MARL #DeepMind #GameTheory #Conventions #Coordination #BehavioralEconomics
