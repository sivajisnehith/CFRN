#set page(
  paper: "a4",
  margin: (top: 2.5cm, bottom: 2.5cm, left: 2.5cm, right: 2.5cm),
  header: align(right)[
    #text(size: 8.5pt, fill: rgb("#666666"))[
      _Springer Nature Journal Style -- CFRN Research Manuscript_
    ]
  ],
  footer: align(center)[
    #context text(size: 9pt)[#counter(page).display("1")]
  ]
)

#set text(
  font: "Times New Roman",
  size: 10pt,
  lang: "en",
)

#set par(
  justify: true,
  leading: 0.65em,
  first-line-indent: 1.2em,
)

// Document Header
#align(center)[
  #v(0.5cm)
  #text(size: 17pt, weight: "bold")[
    Counterfactual Failure-Propagation Debt Optimization for Self-Evolving Network Reconfiguration
  ]
  #v(0.6cm)
  #text(size: 11pt, weight: "bold")[
    Sivaji Snehith#super[1, \*], Datta Sai Charan#super[1], Ahmed Shariff#super[1], Sai Krishna#super[1], Akshay Rajkumar#super[1]
  ]
  #v(0.2cm)
  #text(size: 8.5pt, fill: rgb("#333333"))[
    #super[1] School of Computer Science and Engineering, VIT-AP University, Amaravati, Andhra Pradesh, India\
    #v(2pt)
    *Corresponding author:* `snehith.24bcs7081@vitapstudent.ac.in`\
    #text(size: 8pt, fill: rgb("#555555"))[
      Co-authors: `charan.24bce8127@vitapstudent.ac.in`, `sharif.24bce8418@vitapstudent.ac.in`,\
      `krishna.24bcs7094@vitapstudent.ac.in`, `rajkumar.24bce8597@vitapstudent.ac.in`
    ]
  ]
  #v(0.6cm)
]

// Abstract (Standard Springer Nature Journal Format)
#v(0.2cm)
#line(length: 100%, stroke: 0.5pt + rgb("#bbbbbb"))
#v(0.2cm)
#pad(x: 1cm)[
  #text(weight: "bold", size: 10pt)[Abstract] \
  #v(2pt)
  #text(size: 9pt)[
    Modern large-scale communication backbones are subject to cascading structural and load failures, where an isolated disruption redistributes traffic and triggers secondary overloads across interconnected topologies. Traditional reactive recovery mechanisms either optimize local rerouting without considering cascading debt or deploy heuristic rules that lack anticipatory foresight. In this paper, we propose *CFRN* (_Counterfactual Failure-Propagation Debt Optimization for Self-Evolving Network Reconfiguration_), an autonomous closed-loop framework combining counterfactual reasoning, multi-step cascade simulation, and evolutionary surrogate learning. CFRN formalizes _Counterfactual Propagation Debt_ (CPD) as the discounted cumulative severity of multi-step cascade states over an evaluation horizon, incorporating traffic congestion pressure, topological partitioning, and unserved demand. By generating diverse candidate network mutations---including selective link removal, protective link addition, flow diversion, and articulation path isolation---CFRN evaluates each candidate counterfactual topology using a holistic Counterfactual Propagation-Evolution Score (CES) balancing debt reduction, topological adaptability (algebraic connectivity), and operational cost. Evaluated against internet-scale topologies derived from CAIDA ITDK-2026-03 and realistic traffic dynamics from CESNET-TimeSeries24, CFRN reduces propagation debt by over 12% while preventing catastrophic secondary cascades, providing a principled foundation for self-evolving, self-healing network infrastructures.
  ]
  #v(6pt)
  #text(size: 8.5pt)[
    *Keywords:* Autonomous Reconfiguration, Cascading Failures, Counterfactual Reasoning, Failure Propagation Debt, Self-Evolving Networks.
  ]
]
#v(0.2cm)
#line(length: 100%, stroke: 0.5pt + rgb("#bbbbbb"))
#v(0.4cm)

= 1. Introduction

Critical cyberinfrastructures, ranging from continental telecommunication backbones to hyperscale cloud data centers, operate under stringent availability requirements @caesar2005bgp. Despite substantial hardware redundancy, modern backbones remain susceptible to large-scale cascading breakdowns @motter2002cascade, @dobson2007complex. When a pivotal node or backbone link fails, traffic flows traversing that element are abruptly rerouted onto alternate paths. If alternate paths possess insufficient capacity headroom, downstream links become saturated, causing sequential queue drops, control-plane timeouts, and secondary link failures @albert2000error.

Classical recovery strategies typically follow myopic heuristics: local failure recovery, shortest-path restoration, or static rerouting tables. While these strategies address immediate connectivity loss, they fail to model the "propagation debt" accumulated over subsequent propagation steps. Uncoordinated load shifting often shifts congestion into bottleneck transit paths, triggering severe network partitioning.

To transcend reactive paradigms, autonomous computing systems must incorporate counterfactual anticipation @pearl2009causality, @kephart2003vision. Specifically, an autonomous control plane must ask: _"If mutation $m_i$ were applied immediately following failure $F_t$, how would failure propagation unfold across future time steps relative to the do-nothing baseline?"_

In this work, we introduce the *CFRN* framework (*C*ounterfactual *F*ailure-Propagation Debt *R*econfiguration *N*etwork). CFRN formalizes an 8-stage closed-loop architecture:
$ "Monitor & Detect" -> "Predict & Evaluate" -> "Reconfigure & Evolve" -> "Learn & Adapt" $

The key contributions of this paper are:
+ *Propagation Debt Formulation*: We formalize step-level failure-propagation debt ($D_"FP"$) and time-discounted Counterfactual Propagation Debt ($"CPD"$) across a multi-step cascade simulation horizon.
+ *Counterfactual Mutation Engine*: We define an expressive mutation space spanning link removal, proactive link activation, flow rerouting, and path isolation.
+ *Counterfactual Propagation-Evolution Score (CES)*: We formulate an optimization objective combining debt reduction ($Delta "CPD"$), structural adaptability ($A$), and multidimensional mutation costs ($"MC"$).
+ *Self-Evolution and Empirical Validation*: We incorporate surrogate gradient-boosted prediction and evolutionary action memory, validating performance using CAIDA ITDK-2026-03 @caida_itdk_2026 and CESNET-TimeSeries24 @cesnet_timeseries24 profiles.

= 2. Network Model and Cascading Failure Propagation

== 2.1 Network State Representation
Let the network topology be represented as an undirected graph $G = (V, E)$, where $V$ denotes routing nodes and $E$ denotes transmission links. Each undirected link $e = (u, v) in E$ is characterized by a capacity $c(e) > 0$ and carries an aggregated traffic load $l(e) >= 0$. Link utilization is defined as:
$ u(e) = l(e) / c(e) $
A link is deemed overloaded whenever $u(e) > theta_"over"$, where $theta_"over" >= 1.0$ is the operational threshold. Traffic demand is specified by an origin-destination matrix $cal(D) = {(s_k, d_k, w_k)}_(k=1)^K$, with $w_k$ routed along shortest path $P(s_k, d_k)$ in $G$.

== 2.2 Multi-Step Cascading Failure Mechanics
Upon the occurrence of an initiating failure event $F_t$ (e.g., node removal or high-betweenness link failure), the downed link set $E_"down"^((0))$ is severed from $G$. For each propagation step $tau in {1, dots, H}$, the excess load previously carried by downed links is redistributed onto available alternate shortest paths with redistribution fraction $rho in (0, 1]$.

Redistribution induces new link overloads. At step $tau$, each overloaded link $e$ fails stochastically with failure probability $p_"fail"$:
$ Pr(e in E_"down"^((tau)) mid(|) u(e) > theta_"over") = p_"fail" $

= 3. Counterfactual Propagation Debt (CPD)

== 3.1 Single-Step Propagation Debt
At any cascade step $tau$, the network state severity is quantified by the failure-propagation debt $D_"FP"(G_tau)$:
$ D_"FP"(G_tau) = w_"over" dot Phi_"cong"(G_tau) + w_"disc" dot Psi_"disc"(G_tau) + w_"lost" dot Omega_"lost"(G_tau) $
where $w_"over" + w_"disc" + w_"lost" = 1.0$, and the components are defined as:
- *Congestion Pressure $Phi_"cong"$*: A smooth metric penalizing links exceeding headroom threshold $kappa = 0.75 theta_"over"$:
  $ Phi_"cong"(G) = 1 / (|E|) sum_(e in E) [max(0, u(e) - kappa) + 2 dot max(0, u(e) - theta_"over")] $
- *Disconnected Fraction $Psi_"disc"$*: The proportion of nodes severed from the giant connected component:
  $ Psi_"disc"(G) = (|V| - |V_"giant"|) / (|V|) $
- *Unserved Load Fraction $Omega_"lost"$*: The fraction of total demand whose origin and destination become disconnected:
  $ Omega_"lost"(G) = 1 - (sum_((s, d, w) in cal(D)_"routable") w) / (sum_((s, d, w) in cal(D)) w) $

== 3.2 Cumulative Discounted Debt
Over a look-ahead simulation horizon $H$, Counterfactual Propagation Debt accumulates future step debts with temporal discount factor $gamma in (0, 1]$:
$ "CPD"(G) = sum_(tau=1)^H gamma^(tau - 1) dot D_"FP"(G_tau) $

= 4. Counterfactual Reconfiguration & Optimization

== 4.1 Counterfactual Mutation Space
Following failure $F_t$, CFRN explores candidate counterfactual interventions $cal(M) = {m_1, m_2, dots, m_K}$:
1. *Selective Link Removal (`remove_link`)*: Deliberately deactivates pre-overloaded links to deflect surges away from vulnerable choke points.
2. *Protective Link Addition (`add_link`)*: Establishes auxiliary links between high-centrality, non-adjacent routers.
3. *Flow Rerouting (`reroute_flow`)*: Shifts 50% of traffic off critically stressed links onto candidate bypass paths.
4. *Path Isolation (`isolate_path`)*: Cuts strategic articulation bridges to contain localized cascading failures.

== 4.2 CES Objective Formulation
For each candidate $m_i$, CFRN evaluates:
- *Debt Reduction*: $Delta "CPD"(m_i) = "CPD"_"baseline" - "CPD"(G_t \u{2295} m_i)$.
- *Adaptability $A(m_i)$*: Quantified by the algebraic connectivity (Fiedler eigenvalue $lambda_2$ of the graph Laplacian) of the giant component @fiedler1973algebraic:
  $ A(m_i) = lambda_2(L(G_"giant"^"CF")) $
- *Mutation Cost $"MC"(m_i)$*: A linear combination of link changes ($L$), routing changes ($S$), and effort ($E$):
  $ "MC"(m_i) = alpha dot L(m_i) + beta dot S(m_i) + chi dot E(m_i) $

The *Counterfactual Propagation-Evolution Score (CES)* is:
$ "CES"(m_i) = (Delta "CPD"(m_i) + eta dot A(m_i)) / ("MC"(m_i) + epsilon) $
and the optimal action is $m^* = "arg max"_(m_i in cal(M)) "CES"(m_i)$.

#v(0.3cm)

#align(center)[
  #block(
    width: 100%,
    stroke: (top: 1.2pt + black, bottom: 1.2pt + black),
    inset: (top: 8pt, bottom: 8pt, left: 12pt, right: 12pt),
  )[
    #align(left)[
      #text(weight: "bold", size: 10pt)[Algorithm 1: CFRN Closed-Loop Counterfactual Reconfiguration] \
      #v(1pt)
      #line(length: 100%, stroke: 0.5pt + black)
      #v(2pt)
      #text(size: 8.5pt)[
        *Require:* Base topology state $G_t$, traffic demands $cal(D)$, failure event $F_t$, cascade horizon $H$, cost weights $alpha, beta, chi$, resilience weight $eta$\
        *Ensure:* Optimal mutation $m^*$ and reconfigured network state $G_(t+1)$\
      ]
      #v(1pt)
      #line(length: 100%, stroke: 0.5pt + rgb("#cccccc"))
      #v(2pt)
      #text(size: 8.5pt, font: "Courier New")[
        #grid(
          columns: (22pt, 1fr),
          gutter: 6pt,
          [1:], [$G_"base" <- "ApplyFailure"(G_t, F_t)$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Sever initiating failed element]],
          [2:], [$"CPD"_"base" <- "SimulateCascade"(G_"base", H)$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Project unrestrained baseline cascade]],
          [3:], [$cal(M) <- "GenerateMutations"(G_"base")$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Generate candidate changes (Classes 1-4)]],
          [4:], [*for each* candidate mutation $m_i in cal(M)$ *do*],
          [5:], [$quad G_i^"CF" <- "ApplyMutation"(G_"base", m_i)$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Spawn counterfactual network state]],
          [6:], [$quad "CPD"_i <- "SimulateCascade"(G_i^"CF", H)$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Multi-step cascade simulation]],
          [7:], [$quad Delta "CPD"_i <- "CPD"_"base" - "CPD"_i$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Net propagation debt reduction]],
          [8:], [$quad A_i <- lambda_2(L(G_i^"CF"))$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Future adaptability (algebraic connectivity)]],
          [9:], [$quad "MC"_i <- alpha dot L(m_i) + beta dot S(m_i) + chi dot E(m_i)$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Multidimensional mutation cost]],
          [10:], [$quad "CES"_i <- (Delta "CPD"_i + eta dot A_i) / ("MC"_i + epsilon)$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Compute Counterfactual Evolution Score]],
          [11:], [*end for*],
          [12:], [$m^* <- "arg max"_(m_i in cal(M)) "CES"_i$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Select globally optimal mutation]],
          [13:], [$G_(t+1) <- "ApplyMutation"(G_"base", m^*)$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Enact selected mutation on physical network]],
          [14:], [$"UpdateSurrogateAndMemory"(G_t, F_t, m^*, "CES"^*)$ #h(1fr) #text(fill: rgb("#666666"))[\/\/ Dual-tier self-evolution update]],
          [15:], [*return* $m^*, G_(t+1)$]
        )
      ]
    ]
  ]
]

== 4.3 Algorithmic Walkthrough and Operational Analysis
Algorithm 1 operationalizes the closed-loop decision process through five synchronized phases:
+ *Phase 1: Baseline Propagation Anchor (Lines 1--2)*: Upon detecting failure $F_t$, the element is removed to yield $G_"base"$. Rather than triggering immediate local rerouting, CFRN simulates an unmitigated $H$-step cascade to compute the baseline propagation debt $"CPD"_"base"$, providing a quantifiable counterfactual ground truth.
+ *Phase 2: Multi-Class Mutation Generation (Line 3)*: The mutation generator analyzes topological stress and articulation bridges to propose candidate interventions across four orthogonal classes: selective link removal, proactive link activation, flow diversion, and path isolation.
+ *Phase 3: Parallel Counterfactual Cascade Simulation (Lines 4--6)*: For each candidate $m_i$, an independent counterfactual network world $G_i^"CF" = G_"base" \u{2295} m_i$ is spawned and evaluated over horizon $H$, measuring how the specific mutation reshapes cascading overloads.
+ *Phase 4: Multi-Objective Scoring via CES (Lines 7--11)*: Candidate mutations are evaluated across three complementary objectives: debt reduction $Delta "CPD"_i$, topological adaptability $A_i$ (measured via the Fiedler eigenvalue $lambda_2$ to prevent graph partitioning), and mutation cost $"MC"_i$. The resulting $"CES"_i$ score identifies mutations with maximum benefit per unit reconfiguration effort.
+ *Phase 5: Reconfiguration and Evolutionary Learning (Lines 12--15)*: The optimal candidate $m^* = "arg max" "CES"_i$ is enacted upon the network to produce $G_(t+1)$. The scenario signature and observed outcome are stored in evolutionary memory and used to update the surrogate gradient-boosting model, continuously accelerating future decisions.

= 5. Self-Evolution and Learning Engine

To accelerate decision-making over prolonged operations, CFRN maintains an evolutionary knowledge base operating on two tiers @friedman2001greedy:
+ *Surrogate Model*: A Gradient Boosted Regressor trained on topological graph features (node count, link count, density, average clustering, current overload count) to predict post-failure CPD without requiring full multi-step simulation.
+ *Evolutionary Memory*: A persistent case-based action bank mapping scenario signatures $Sigma = ("failure_type", |V|/10, |E_"over"|)$ to historically successful mutation types, prioritizing high-payoff interventions for recurring disruption patterns.

= 6. Experimental Evaluation

== 6.1 Datasets and Experimental Setup
Experiments were executed across two benchmark sources matching the operational plan:
- *CAIDA ITDK-2026-03* @caida_itdk_2026: Internet Topology Data Kit router-level connectivity, supplemented with synthetic Barabási--Albert scale-free graphs ($n=120, m=2$) matching empirical Internet degree power laws @barabasi1999emergence.
- *CESNET-TimeSeries24* @cesnet_timeseries24: Empirical time-series traffic distributions incorporating diurnal and weekly seasonality, bursty log-normal traffic volumes, and shortest-path OD assignments.

#v(0.3cm)
#align(center)[
  #table(
    columns: (2.5fr, 1.5fr, 1.5fr, 1.5fr, 1.8fr),
    inset: 7pt,
    align: (left, center, center, center, center),
    stroke: (x, y) => if y == 0 { (bottom: 1pt + black, top: 1pt + black) } else if y == 5 { (bottom: 1pt + black) } else { (bottom: 0.5pt + rgb("#e0e0e0")) },
    table.header([*Metric*], [*Baseline*], [*CFRN ($m^*$)*], [*Reduction*], [*Relative Gain*]),
    [Mean Propagation Debt (CPD)], [0.0864], [0.0758], [0.0106], [*+12.3%*],
    [Secondary Links Failed], [1.42], [1.05], [0.37], [*+26.1%*],
    [Peak Overloaded Links], [3.84], [2.18], [1.66], [*+43.2%*],
    [Connectivity Loss ($Psi_"disc"$)], [0.048], [0.012], [0.036], [*+75.0%*],
    [Mean Recovery Steps], [2.14], [1.38], [0.76], [*+35.5%*],
  )
  #text(size: 8.5pt, style: "italic")[Table 1: Empirical evaluation comparing Baseline (do-nothing) vs CFRN reconfiguration across 60 failure scenarios.]
]

#v(0.4cm)

== 6.2 Empirical Visualizations

#grid(
  columns: (1fr, 1fr),
  gutter: 12pt,
  [
    #image("CFRN/results/cpd_comparison.png", width: 100%)
    #align(center)[#text(size: 8pt)[Figure 1: Scenario-by-scenario CPD comparison]]
  ],
  [
    #image("CFRN/results/cascade_size.png", width: 100%)
    #align(center)[#text(size: 8pt)[Figure 2: Mean cascade size reduction]]
  ]
)

#v(0.3cm)
#align(center)[
  #image("CFRN/results/mutation_selection.png", width: 60%)
  #text(size: 8pt)[Figure 3: Frequency distribution of selected counterfactual mutation types ($m^*$)]
]

= 7. Discussion and Limitations
CFRN shows substantial improvements in mitigating secondary cascades by actively shaping traffic flow and topology before overloads spread. Methodologically, as highlighted in Section 11 of the project roadmap, CAIDA and CESNET capture distinct physical infrastructures; CFRN uses CESNET to model realistic statistical load distributions applied across CAIDA-derived topologies. In operational settings, control-plane reconfiguration latency must be considered against cascade propagation timescales.

= 8. Conclusion
In this paper, we presented *CFRN*, a closed-loop framework for self-evolving network reconfiguration driven by counterfactual propagation debt optimization. By systematically projecting cascading consequences across candidate topological mutations, CFRN significantly suppresses secondary outages, preserves network connectivity, and accelerates recovery. Future extensions will investigate distributed multi-agent counterfactual planning for software-defined autonomous systems.

#v(0.5cm)
#line(length: 100%, stroke: 0.5pt + rgb("#cccccc"))
#text(size: 9pt)[
  *Declarations:* Funding: Not applicable. Conflict of interest: None declared. Data availability: CAIDA and CESNET datasets are publicly available. Code availability: All implementation code and interactive dashboards are in the root directory.
]

#v(0.5cm)
#bibliography("sn-bibliography.bib", style: "springer-mathphys")
