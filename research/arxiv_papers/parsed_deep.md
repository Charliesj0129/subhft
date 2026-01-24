# Deep Parsed Index: arxiv_papers

## 2601.01189v1 - Central limit theorem for a partially observed interacting system of Hawkes processes I: subcritical case
- Date: 2026-01-03
- PDF: ./2601.01189v1_Central_limit_theorem_for_a_pa.pdf
- Models/Methods: Hawkes Process
- Evidence: Theory
- Domain: General
- Abstract: We consider a system ofNHawkes processes and observe the actions of a subpopulation of sizeKNup to timet, whereKis large. The influence relationships between each pair of individuals are modeled by i.i.d.Bernoulli(p) random variables, wherep[0,1] is an unknown parameter. Each individual acts at abaselinerate >0 and, additionally, at an excitationrate of the formN 1 PN j=1 ij R t 0 (ts)dZ j,N s , which depends on the past actions of all individuals that influence it, scaled byN 1 (i.e.
- Contributions: We consider a system ofNHawkes processes and observe the actions of a subpopulation of sizeKNup to timet, whereKis large.
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2511.18117v1 - Diffusive Limit of Hawkes Driven Order Book Dynamics With Liquidity Migration
- Date: 2025-11-22
- PDF: ./2511.18117v1_Diffusive_Limit_of_Hawkes_Driv.pdf
- Models/Methods: Hawkes Process, Multivariate Hawkes
- Evidence: Unclear
- Domain: LOB, Order Flow
- Keywords: Limit Order Book, Hawkes Processes, Functional Central Limit
- Abstract: This paper develops a theoretical mesoscopic model of the limit order book driven by multivariate Hawkes processes, designed to capture temporal self-excitation and the spatial propagation of order flow across price levels. In contrast to classicalzero-intelligenceor Poisson based queueing models, the proposed framework introduces mathematically defined migration events between neighbouring price levels, whose intensities are themselves governed by the underlying Hawkes structure. This provides a principled stochastic mechanism for modeling interactions between order arrivals, cancellations, and liquidity movement across adjacent queues.
- Contributions: This paper develops a theoretical mesoscopic model of the limit order book driven by multivariate Hawkes processes, designed to capture temporal self-excitation and the spatial propagation of order flow across price levels. In this work we have developed a mesoscopic description of the bid-side queue dynamics in a limit order book driven by a multivariate Hawkes process. Starting from a fully microscopic specification in which individual order arrivals, cancellations and migrations are encoded as components of a Hawkes counting process, we introduced a diffusive rescaling under which time is accelerated by a factornand queue sizes are of order n. In this regime, and under suitable equilibrium intensity expansions, we derived the infinitesimal generator of the rescaled queue process and showed that it converges to the generator of a reflected diffusion onR N1 + .
- Conclusion: In this work we have developed a mesoscopic description of the bid-side queue dynamics in a limit order book driven by a multivariate Hawkes process. Starting from a fully microscopic specification in which individual order arrivals, cancellations and migrations are encoded as components of a Hawkes counting process, we introduced a diffusive rescaling under which time is accelerated by a factornand queue sizes are of order n. In this regime, and under suitable equilibrium intensity expansions, we derived the infinitesimal generator of the rescaled queue process and showed that it converges to the generator of a reflected diffusion onR N1 + .
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2510.26438v2 - An Impulse Control Approach to Market Making in a Hawkes LOB Market
- Date: 2025-10-30
- PDF: ./2510.26438v2_An_Impulse_Control_Approach_to.pdf
- Models/Methods: Hawkes Process, Mutually Exciting, Price Impact, Impulse Control, Market Making
- Evidence: Unclear
- Domain: LOB, Market Making
- Abstract: We study the optimal Market Making problem in a Limit Order Book (LOB) market simulated using a high-fidelity, mutually exciting Hawkes process. Departing from traditional Brownian-driven midprice models, our setup captures key microstructural properties such as queue dynamics, inter-arrival clustering, and endogenous price impact. Recognizing the realistic constraint that market makers cannot update strategies at every LOB event, we formulate the control problem within an impulse control framework, where interventions occur discretely via limit, cancel, or market orders.
- Intro highlights: Market Making in Limit Order Books (LOBs) is a high frequency trading task, where liquidity is provided with the goal of capturing the bid-ask spread. While many classical formulations rely on continuous-time dynamics and control, the true microstructure of the LOB is inherently discrete and driven by a pure jump process. The distinction between liquidity provision and liquidity consumption represents a fundamental dichotomy in modern market microstructure theory.
- Contributions: We study the optimal Market Making problem in a Limit Order Book (LOB) market simulated using a high-fidelity, mutually exciting Hawkes process. Recognizing the realistic constraint that market makers cannot update strategies at every LOB event, we formulate the control problem within an impulse control framework, where interventions occur discretely via limit, cancel, or market orders.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2510.21297v1 - Jump risk premia in the presence of clustered jumps
- Date: 2025-10-24
- PDF: ./2510.21297v1_Jump_risk_premia_in_the_presen.pdf
- Models/Methods: Hawkes Process, Option Pricing
- Evidence: Unclear
- Domain: Options/VIX, Volatility, Crypto
- Keywords: Volatility risk premium, clustered jumps, Hawkes process, cryptocurrencies
- Abstract: This paper presents an option pricing model that incorporates clustered jumps using a bivariate Hawkes process. The process captures both selfand cross-excitation of positive and negative jumps, enabling the model to generate return dynamics with asymmetric, time-varying skewness and to produce positive or negative implied volatility skews. This feature is especially relevant for assets such as cryptocurrencies, so-called meme stocks, G-7 currencies, and certain commodities, where implied volatility skews may change sign depending on prevailing sentiment.
- Intro highlights: Jumps are acknowledged as an important risk factor in asset pricing, derivatives pricing, and risk management. Although there is a large body of literature investigating jumps in financial markets for traditional assets, the emergence of option markets for cryptocurrencies and retail-driven trading activity in options on meme stocks provide a new environment for this field of study. In this paper, we focus on the dynamics and options market for cryptocurrencies and, specifically, for Bitcoin (BTC).
- Contributions: This paper presents an option pricing model that incorporates clustered jumps using a bivariate Hawkes process.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2510.08085v1 - A Deterministic Limit Order Book Simulator with Hawkes-Driven Order Flow
- Date: 2025-10-09
- PDF: ./2510.08085v1_A_Deterministic_Limit_Order_Bo.pdf
- Models/Methods: Hawkes Process, Marked Hawkes, Order Book Simulator
- Evidence: Theory, Empirical, Simulation
- Domain: LOB, Order Flow
- Keywords: Hawkes processes, limit order book, market microstructure, high-frequency trading,
- Abstract: We present a reproducible research stack for market microstructure: a modern C++ deterministic limit order book (LOB) engine, a multivariate marked Hawkes order-flow generator exposed to both C++ and Python, and a set of diagnostics and benchmarks released with code and artifacts. On the theory side, we recall stability conditions for linear and nonlinear Hawkes processes, provide complete proofs with intuitive explanations, and use the time-rescaling theorem to build goodness-of-fit tests. Empirically, we calibrate and compare exponential vs.
- Contributions: We present a reproducible research stack for market microstructure: a modern C++ deterministic limit order book (LOB) engine, a multivariate marked Hawkes order-flow generator exposed to both C++ and Python, and a set of diagnostics and benchmarks released with code and artifacts. On the theory side, we recall stability conditions for linear and nonlinear Hawkes processes, provide complete proofs with intuitive explanations, and use the time-rescaling theorem to build goodness-of-fit tests. Empirically, we calibrate and compare exponential vs. We presented a reproducible framework for simulating limit order book dynamics driven by marked Hawkes processes.
- Conclusion: We presented a reproducible framework for simulating limit order book dynamics driven by marked Hawkes processes. Our contributions span theory (complete proofs with intuition), implementation (C++/Python simulator), and empirics (benchmarks on Binance and LOBSTER data). Key takeaways: Hawkes processes provide a mathematically rigorous and empirically validated approach to modeling order flow clustering The nearly-unstable regime ((G)1) is crucial for realistic dynamics Time-rescaling diagnostics offer powerful goodness-of-fit tests Future work should integrate LOB state dependence for improved realism All code, data, and configurations are available at: https://github.com/sohaibelkarmi/High-Frequency-Trading-Simulator References [1] Alan G.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2510.06879v1 - Nonparametric Estimation of Self- and Cross-Impact
- Date: 2025-10-08
- PDF: ./2510.06879v1_Nonparametric_Estimation_of_Se.pdf
- Models/Methods: Propagator Model, Price Impact
- Evidence: Empirical
- Domain: Execution, Volatility, Order Flow
- Keywords: market impact, cross-impact, concave price impact, nonparametric estimation,
- Abstract: Weintroduceanofflinenonparametricestimatorforconcavemulti-assetpropagatormodels based on a dataset of correlated price trajectories and metaorders. Compared to parametric models, our framework avoids parameter explosion in the multi-asset case and yields confidence bounds for the estimator. We implement the estimator using both proprietary metaorder data from Capital Fund Management (CFM) and publicly available S&P order flow data, where we augment the former dataset using a metaorder proxy.
- Intro highlights: Price impact refers to the empirical observation that executing a large order adversely affects the price of a risky asset in a persistent manner, resulting in less favorable execution prices. It is well documented that price impact is concave with respect to trade size: larger trades tend to move prices less per unit volume than smaller trades, a property not captured by linear models. Instead, the empirical literature supports a square-root law of market impact [8, 10, 31, 41, 45], where the peak impact induced by a largemetaorderof sizeQis given by I peak =Y  Dsign(Q) Q VD  ,(1.1) whereYis a constant of order one,V D is the total daily traded volume, andD is the daily volatility.
- Contributions: We implement the estimator using both proprietary metaorder data from Capital Fund Management (CFM) and publicly available S&P order flow data, where we augment the former dataset using a metaorder proxy.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2509.21244v1 - Multivariate Quadratic Hawkes Processes -- Part II: Non-Parametric Empirical Calibration
- Date: 2025-09-25
- PDF: ./2509.21244v1_Multivariate_Quadratic_Hawkes_.pdf
- Models/Methods: Hawkes Process, Quadratic Hawkes
- Evidence: Empirical
- Domain: General
- Keywords: Multivariate QHawkes, QGARCH, non-parametric calibration
- Abstract: This is the second part of our work on Multivariate Quadratic Hawkes (MQHawkes) Processes, devoted to the calibration of the model defined and studied analytically in Aubrun, C., Benzaquen, M., & Bouchaud, J. P., Quantitative Finance, 23(5), 741-758 (2023). We propose a non-parametric calibration me...
- Intro highlights: The increasing amount of high-frequency data in financial markets has made it possible to study and calibrate microstructure models. An increasingly renowned familyofsuchmodelshasbeeninventedbythelateAlan G. Hawkes [1, 2].
- Contributions: We propose a non-parametric calibration me...
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2508.16566v1 - Asymmetric super-Heston-rough volatility model with Zumbach effect as scaling limit of quadratic Hawkes processes
- Date: 2025-08-22
- PDF: ./2508.16566v1_Asymmetric_super-Heston-rough_.pdf
- Models/Methods: Hawkes Process, Quadratic Hawkes, Rough Volatility
- Evidence: Unclear
- Domain: Volatility
- Abstract: Hawkes processes were first introduced to obtain microscopic models for the rough volatility observed in asset prices. Scaling limits of such processes leads to the rough-Heston model that describes the macroscopic behavior. Blanc et al.
- Intro highlights: The hunt for a perfect statistical model of financial markets is still going on, wrote the authors in [ 2]. The goal is to derive models for macroscopic behavior of financial asset prices that arise as scaling limits of stochastic models that incorporate micro-structural features observed *priyankac@iisc.ac.in Corresponding author: skiyer@iisc.ac.in 1 arXiv:2508.16566v1 [q-fin.ST] 22 Aug 2025 in time series of price processes (see [ 1, 2, 6, 12, 15, 24]). Properties of interest include volatility clustering, leverage effect, fat (power-law) tails of the return distribution, etc.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2508.16589v1 - ARL-Based Multi-Action Market Making with Hawkes Processes and Variable Volatility
- Date: 2025-08-07
- PDF: ./2508.16589v1_ARL-Based_Multi-Action_Market_.pdf
- Models/Methods: Hawkes Process, Market Making, Reinforcement Learning
- Evidence: Unclear
- Domain: Market Making, Volatility
- Abstract: We advance market-making strategies by integrating Adversarial Reinforcement Learning (ARL), Hawkes Processes, and variable volatility levels while also expanding the action space available to market makers (MMs). To enhance the adaptability and robustness of these strategies  which can quote always, quote only on one side of the market or not quote at all  we shift from the commonly used Poisson process to the Hawkes process, which better captures real market dynamics and self-exciting behaviors. We then train and evaluate strategies under volatility levels of 2 and 200.
- Intro highlights: Market makers (MMs) are integral to financial markets, enhancing liquidity and stability by consistently providing bid and ask quotes, which promotes efficient and orderly trading. While they derive profits from the bid-ask spread, they face significant challenges due to the complexity of market environments and price volatility. Recent research has focused extensively on risk management and Permission to make digital or hard copies of part or all of this work for personal or classroom use is granted without fee provided that copies are not made or distributed for profit or commercial advantage and that copies bear this notice and the full citation on the first page.
- Contributions: We advance market-making strategies by integrating Adversarial Reinforcement Learning (ARL), Hawkes Processes, and variable volatility levels while also expanding the action space available to market makers (MMs). To enhance the adaptability and robustness of these strategies which can quote always, quote only on one side of the market or not quote at all we shift from the commonly used Poisson process to the Hawkes process, which better captures real market dynamics and self-exciting behaviors. We then train and evaluate strategies under volatility levels of 2 and 200.
- Conclusion: This study highlights the effectiveness of integrating Adversarial Reinforcement Learning (ARL) with sophisticated modeling techniques such as Hawkes Processes and variable volatility levels to advance market-making strategies. By expanding the action space and employing a more realistic representation of market dynamics, our research demonstrates that market makers show significant resilience and adaptability across different volatility regimes. The 4-Action MM (Train @ vol=2, Test @ vol=200) performs stably in high-volatility environments due to its training strategys strong adaptability and robustness.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2506.07711v5 - The Subtle Interplay between Square-root Impact, Order Imbalance & Volatility: A Unifying Framework
- Date: 2025-06-09
- PDF: ./2506.07711v5_The_Subtle_Interplay_between_S.pdf
- Models/Methods: None detected
- Evidence: Unclear
- Domain: Volatility, Order Flow
- Abstract: In this work, we aim to reconcile several apparently contradictory observations in market microstructure: is the famous square-root law of metaorder impact, which decays with time, compatible with the random-walk nature of prices and the linear impact of order imbalances? Can one entirely explain the volatility of prices as resulting from the flow of uninformed metaorders that mechanically impact them? We introduce a new theoretical framework to describe metaorders with different signs, sizes and durations, whichall impact prices as a square-root of volume but with a subsequent time decay.
- Intro highlights: 3 2 A Continuous Time Description of the Order Flow 4 2.1 Model set-up . . .
- Contributions: In this work, we aim to reconcile several apparently contradictory observations in market microstructure: is the famous square-root law of metaorder impact, which decays with time, compatible with the random-walk nature of prices and the linear impact of order imbalances? We introduce a new theoretical framework to describe metaorders with different signs, sizes and durations, whichall impact prices as a square-root of volume but with a subsequent time decay. The aim of this paper was to reconcile several apparently contradictory observations: is a square-root law of metaorder impact that decays with time compatible with the random-walk nature of prices and the linear impact of order imbalances? In order to answer these questions, we have introduced a new theoretical framework to describe metaorders with different signs, sizes and durations, possibly correlated between themselves, which all impact prices as a square-root of volume (which we assume as an input) but with a subsequent time decay characterized by an exponent = 1 2, i.e.
- Conclusion: The aim of this paper was to reconcile several apparently contradictory observations: is a square-root law of metaorder impact that decays with time compatible with the random-walk nature of prices and the linear impact of order imbalances? Can one entirely explain the volatility of prices as resulting from a soup of indistinguishable, randomly intertwined and uninformed metaorders? In order to answer these questions, we have introduced a new theoretical framework to describe metaorders with different signs, sizes and durations, possibly correlated between themselves, which all impact prices as a square-root of volume (which we assume as an input) but with a subsequent time decay characterized by an exponent  = 1 2, i.e.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2505.17388v1 - Stochastic Price Dynamics in Response to Order Flow Imbalance: Evidence from CSI 300 Index Futures
- Date: 2025-05-23
- PDF: ./2505.17388v1_Stochastic_Price_Dynamics_in_R.pdf
- Models/Methods: Order Flow Imbalance
- Evidence: Unclear
- Domain: Futures, Order Flow
- Keywords: Order flow imbalance, Ornstein-Uhlenbeck process, SDE, Market microstructure
- Abstract: We conduct modeling of the price dynamics following order flow imbalance in market microstructure and apply the model to the analysis of Chinese CSI 300 Index Futures. There are three findings. The first is that the order flow imbalance is analogous to a shock to the market.
- Contributions: We conduct modeling of the price dynamics following order flow imbalance in market microstructure and apply the model to the analysis of Chinese CSI 300 Index Futures.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2504.15908v1 - Learning the Spoofability of Limit Order Books With Interpretable Probabilistic Neural Networks
- Date: 2025-04-22
- PDF: ./2504.15908v1_Learning_the_Spoofability_of_L.pdf
- Models/Methods: Hawkes Process, Marked Hawkes
- Evidence: Unclear
- Domain: LOB, Crypto, Order Flow
- Keywords: High-Frequency, Market Manipulation, Spoofing, Neural Network, Limit Order Book,
- Abstract: This paper investigates real-time detection of spoofing activity in limit order books, focusing on cryptocurrency centralized exchanges. We first introduce novel order flow variables based on multiscale Hawkes processes that account both for the size and placement distance from current best prices of new limit orders. Using a Level-3 data set, we train a neural network model to predict the conditional probability distribution of mid price movements based on these features.
- Intro highlights: 1 2 An order-driven price formation model 4 2.1 A marked Hawkes-inspired set of features . . .
- Contributions: This paper investigates real-time detection of spoofing activity in limit order books, focusing on cryptocurrency centralized exchanges. We first introduce novel order flow variables based on multiscale Hawkes processes that account both for the size and placement distance from current best prices of new limit orders. Using a Level-3 data set, we train a neural network model to predict the conditional probability distribution of mid price movements based on these features. In this work, we tackle the problem of real-time spoofing detection in a high-frequency trading environment.
- Conclusion: In this work, we tackle the problem of real-time spoofing detection in a high-frequency trading environment. Our focus was on a simple spoofing strategy which consists of posting a large order on one side of the order book in order to inflate the latent liquidity in the book and ultimately mislead other market participants. We introduced novel order flow variables that are built on the intensity of self-exciting point processes.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2504.10282v2 - Optimal Execution in Intraday Energy Markets under Hawkes Processes with Transient Impact
- Date: 2025-04-14
- PDF: ./2504.10282v2_Optimal_Execution_in_Intraday_.pdf
- Models/Methods: Hawkes Process, Mutually Exciting, Price Impact, Optimal Execution
- Evidence: Empirical
- Domain: Execution, Volatility, Energy, Order Flow
- Abstract: This paper investigates optimal execution strategies in intraday energy markets through a mutually exciting Hawkes process model. Calibrated to data from the German intraday electricity market, the model effectively captures key empirical features, including intra-session volatility, distinct intraday market activity patterns, and the Samuelson effect as gate closure approaches. By integrating a transient price impact model with a bivariate Hawkes process to model the market order flow, we derive an optimal trading trajectory for energy companies managing large volumes, accounting for thespecifictradingpatternsofthesemarkets.Aback-testinganalysiscomparestheproposedstrategy against standard benchmarks such as Time-Weighted Average Price (TWAP) and Volume-Weighted Average Price (VWAP), demonstrating substantial cost reductions across various hourly trading products in intraday energy markets.
- Intro highlights: Intraday markets playa crucial role in developedenergy markets,withtradingvolumesreachingnewpeaksinrecent years,asobservedintheGermanmarketin2024[21].Oneof theprimarydriversbehindthisgrowthistheincreasingshare of renewable energy, which requires market participants to balancetheirpositionsinshort-termintradaymarketsdueto thevariableanduncertainproductionfromsourceslikewind and solar. This behavior results in higher trading volumes and elevated price volatility as delivery time approaches, a well-documented phenomenon in commodity and energy markets known as the Samuelson effect [34]. Empirical studies, including [24] and [29], have validated the Samuelson effect in short-term energy markets.
- Contributions: This paper investigates optimal execution strategies in intraday energy markets through a mutually exciting Hawkes process model. By integrating a transient price impact model with a bivariate Hawkes process to model the market order flow, we derive an optimal trading trajectory for energy companies managing large volumes, accounting for thespecifictradingpatternsofthesemarkets.Aback-testinganalysiscomparestheproposedstrategy against standard benchmarks such as Time-Weighted Average Price (TWAP) and Volume-Weighted Average Price (VWAP), demonstrating substantial cost reductions across various hourly trading products in intraday energy markets.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2504.03445v2 - A stochastic volatility approximation for a tick-by-tick price model with mean-field interaction
- Date: 2025-04-04
- PDF: ./2504.03445v2_A_stochastic_volatility_approx.pdf
- Models/Methods: Hawkes Process, Mutually Exciting, Rough Volatility, Mean-Field, Option Pricing
- Evidence: Unclear
- Domain: Options/VIX, Volatility
- Keywords: Stochastic Volatility, Hawkes processes, multifractality, mean-field, non-linearity, criticality
- Abstract: We consider a tick-by-tick model of price formation, in which buy and sell orders are modeled as self-exciting point processes (Hawkes process), similar to the one in [Hoffmann, Bacry, Delattre, Muzy,Modelling microstructure noise with mutually exciting point processes, Quantitative Finance, 2013] and [El Euch, Fukasawa, Rosenbaum,The microstructural foundations of leverage effect and rough volatility, Finance and Stochastics, 2018]. We adopt an agent based approach by studying the aggregation of a large number of these point processes, mutually interacting in a mean-field sense. The financial interpretation is that of an asset on which several labeled agents place buy and sell orders following these point processes, influencing the price.
- Intro highlights: We consider a tick-by-tick model of price formation, in which price variations are due to buy and sell orders of individual agents, that are modeled as self-exciting point processes (Hawkes process), and are mutually interacting in the mean-field sense. Our main aim is to use this model to provide a microscopic foundation to stochastic volatility models in which the mean reversion of the volatility isfaster-thanlinear. Supported by econometric evidence [5], models with quadratic mean reversion in the volatility process have been used for option pricing (e.g.
- Contributions: We consider a tick-by-tick model of price formation, in which buy and sell orders are modeled as self-exciting point processes (Hawkes process), similar to the one in [Hoffmann, Bacry, Delattre, Muzy,Modelling microstructure noise with mutually exciting point processes, Quantitative Finance, 2013] and [El Euch, Fukasawa, Rosenbaum,The microstructural foundations of leverage effect and rough volatility, Finance and Stochastics, 2018]. We adopt an agent based approach by studying the aggregation of a large number of these point processes, mutually interacting in a mean-field sense.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2503.18259v5 - Rough Heston model as the scaling limit of bivariate cumulative heavy-tailed INAR processes: Weak-error bounds and option pricing
- Date: 2025-03-24
- PDF: ./2503.18259v5_Rough_Heston_model_as_the_scal.pdf
- Models/Methods: Hawkes Process, Rough Heston, Rough Volatility, Option Pricing, INAR
- Evidence: Empirical, Simulation
- Domain: Options/VIX, Volatility
- Keywords: Rough volatility, INAR(), scaling limit, FFT-based simulation, implied volatility surface, weak-
- Abstract: This paper links nearly unstable, heavy-tailedbivariate cumulativeINAR() processes to the rough Heston model via a discrete scaling limit, extending scaling-limit techniques beyond Hawkes processes and providing a microstructural mechanism for rough volatility and leverage effect. Computationally, we simulate theapproximating INAR()sequence rather than discretizing the Volterra SDE, and implement the long-memory convolution with adivide-and-conquer FFT(CDQ) that reuses past transforms, yielding an efficient Monte Carlo engine forEuropean optionsandpath-dependent options(Asian, lookback, barrier). We further derive finite-horizonweak-error boundsfor option pricing under our microstructural approximation.
- Intro highlights: The rough Heston model proposed by El Euch and Rosenbaum (2019) is a one-dimensional stochastic volatility model in which the asset priceShas the following dynamic: dSt =S t p VtdWt, Vt =V 0 + 1 () Z t 0 (ts) 1(V s)ds+ 1 () Z t 0 (ts) 1 p VsdBs, where, , , V 0 are positive constants,WandBare two standard Brownian motions with correlation. The parameter(1/2,1) governs the smoothness of the volatility sample path. It is a modified version of the celebrated Heston model proposed by Heston (1993), motivated from empirical observations of roughness in volatility time series; see Gatheral et al.
- Contributions: This paper links nearly unstable, heavy-tailedbivariate cumulativeINAR() processes to the rough Heston model via a discrete scaling limit, extending scaling-limit techniques beyond Hawkes processes and providing a microstructural mechanism for rough volatility and leverage effect. Computationally, we simulate theapproximating INAR()sequence rather than discretizing the Volterra SDE, and implement the long-memory convolution with adivide-and-conquer FFT(CDQ) that reuses past transforms, yielding an efficient Monte Carlo engine forEuropean optionsandpath-dependent options(Asian, lookback, barrier). We further derive finite-horizonweak-error boundsfor option pricing under our microstructural approximation.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2503.14814v1 - Modelling High-Frequency Data with Bivariate Hawkes Processes: Power-Law vs. Exponential Kernels
- Date: 2025-03-19
- PDF: ./2503.14814v1_Modelling_High-Frequency_Data_.pdf
- Models/Methods: Hawkes Process
- Evidence: Unclear
- Domain: LOB, Execution, Order Flow
- Abstract: ThisstudyexplorestheapplicationofHawkesprocessestomodelhigh-frequency datainthecontextoflimitorderbooks. TwodistinctHawkes-basedmodelsare proposed and analyzed: one utilizing exponential kernels and the other employing power-law kernels. These models are implemented within a bivariate framework.
- Intro highlights: High-frequency trading (HFT) has become a dominant force in modern financial markets,characterizedbyrapidorderexecution,lowlatency,andlargevolumesofdata. At the heart of HFT lies the limit order book (LOB), a system that records all buy and sell orders for a given financial instrument at various price levels [1]. Understanding the complexmechanismsofLOBsiscrucialformarketparticipants,asitprovidesinsights into price formation, market liquidity, and the impact of order flow on asset prices.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2503.04323v1 - Fredholm Approach to Nonlinear Propagator Models
- Date: 2025-03-06
- PDF: ./2503.04323v1_Fredholm_Approach_to_Nonlinear.pdf
- Models/Methods: Propagator Model, Price Impact
- Evidence: Empirical
- Domain: Execution
- Keywords: Optimal trading, nonlinear market impact, propagator model, power-law decay, square
- Abstract: We formulate and solve an optimal trading problem with alpha signals, where transactions induce a nonlinear transient price impact described by a general propagator model, including power-law decay. Using a variational approach, we demonstrate that the optimal trading strategy satisfies a nonlinear stochastic Fredholm equation with both forward and backward coefficients. We prove the existence and uniqueness of the solution under a monotonicity condition reflecting the nonlinearity of the price impact.
- Intro highlights: Price impact refers to the empirical observation that executing a large order adversely affects the price of a risky asset in a persistent manner, leading to less favorable execution prices. Consequently, an agent seeking to liquidate a large order, known as a metaorder, must split it into smaller parts, referred to as child orders, which are typically executed over a period of hours or days. A fundamental question in this context concerns the impact of a metaorder as a function of its size.
- Contributions: We formulate and solve an optimal trading problem with alpha signals, where transactions induce a nonlinear transient price impact described by a general propagator model, including power-law decay. Using a variational approach, we demonstrate that the optimal trading strategy satisfies a nonlinear stochastic Fredholm equation with both forward and backward coefficients. We prove the existence and uniqueness of the solution under a monotonicity condition reflecting the nonlinearity of the price impact.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2502.17417v1 - Event-Based Limit Order Book Simulation under a Neural Hawkes Process: Application in Market-Making
- Date: 2025-02-24
- PDF: ./2502.17417v1_Event-Based_Limit_Order_Book_S.pdf
- Models/Methods: Hawkes Process, Neural Hawkes, Market Making
- Evidence: Simulation
- Domain: LOB, Market Making
- Keywords: Algorithmic and High-Frequency Trading, Limit Order Books,
- Abstract: In this paper, we propose an event-driven Limit Order Book (LOB) model that captures twelve of the most observed LOB events in exchangebased financial markets. To model these events, we propose using the state-of-the-art Neural Hawkes process, a more robust alternative to traditional Hawkes process models. More specifically, this model captures the dynamic relationships between different event types, particularly their longand short-term interactions, using a Long Short-Term Memory neural network.
- Intro highlights: There are many works devoted to modeling limit order book (LOB) data, which often follow some form of approximation process for the midprice1 process. A detailed overview of LOB models can be found in the survey paper by Gould et al. (2013), and also in a more recent study by Jain et al.
- Contributions: In this paper, we propose an event-driven Limit Order Book (LOB) model that captures twelve of the most observed LOB events in exchangebased financial markets. To model these events, we propose using the state-of-the-art Neural Hawkes process, a more robust alternative to traditional Hawkes process models. and Future Recommendations In this research, we developed an event-based Neural HP for simulating asset price process at a high-frequency level. Granular LOB information is essential for HFT strategies like for a MM, and we believe our model takes strides in improving analysis in this area.
- Conclusion: and Future Recommendations In this research, we developed an event-based Neural HP for simulating asset price process at a high-frequency level. Granular LOB information is essential for HFT strategies like for a MM, and we believe our model takes strides in improving analysis in this area. More specifically, we developed an event-based LOB model that takes into account 12 of the main events that appear in the LOB, where each events intensity was modeled via a nonlinear MVHP.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2502.04027v1 - High-Frequency Market Manipulation Detection with a Markov-modulated Hawkes process
- Date: 2025-02-06
- PDF: ./2502.04027v1_High-Frequency_Market_Manipula.pdf
- Models/Methods: Hawkes Process, Markov-Modulated
- Evidence: Unclear
- Domain: Crypto
- Keywords: Hawkes process, Regime switching, Cryptocurrency, Wash trading, Ramping,
- Abstract: This work focuses on a self-exciting point process defined by a Hawkes-like intensity and a switching mechanism based on a hidden Markov chain. Previous works in such a setting assume constant intensities between consecutive events. We extend the model to general Hawkes excitation kernels that are piecewise constant between events.
- Contributions: We extend the model to general Hawkes excitation kernels that are piecewise constant between events.
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2501.15106v1 - In-Context Operator Learning for Linear Propagator Models
- Date: 2025-01-25
- PDF: ./2501.15106v1_In-Context_Operator_Learning_f.pdf
- Models/Methods: Propagator Model, Price Impact
- Evidence: Unclear
- Domain: Execution
- Abstract: We study operator learning in the context of linear propagator models for optimal order execution problems with transient price impact ` a la Bouchaud et al. (2004) and Gatheral (2010). Transient price impact persists and decays over time according to some propagator kernel.
- Intro highlights: Devising optimal order execution strategies for buying or selling large volumes of shares of a stock on a centralized exchange is a major concern for large institutional investors and hence became a well-studied problem in financial mathematics in the past two decades. The aim is to split up a large meta order into smaller child orders which are executed over some time horizon to mitigate adverse price impact incurred by large trades. We refer to the excellent monographs [CJP15, Gu e16, BBDG18, Web23] for a comprehensive overview of this topic.
- Contributions: We study operator learning in the context of linear propagator models for optimal order execution problems with transient price impact ` a la Bouchaud et al.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2412.15172v1 - Option Pricing with a Compound CARMA(p,q)-Hawkes
- Date: 2024-12-19
- PDF: ./2412.15172v1_Option_Pricing_with_a_Compound.pdf
- Models/Methods: Hawkes Process, CARMA-Hawkes, Option Pricing
- Evidence: Empirical
- Domain: Options/VIX, Volatility
- Abstract: A self-exciting point process with a continuous-time autoregressive moving average intensity process, named CARMA(p,q)-Hawkes model, has recently been introduced. The model generalizes the Hawkes process by substituting the Ornstein-Uhlenbeck intensity with a CARMA(p,q) model where the associated state process is driven by the counting process itself. The proposed model preserves the same degree of tractability as the Hawkes process, but it can reproduce more complex time-dependent structures observed in several market data.
- Intro highlights: In both academic and financial industry literature it is well established that the Black-Scholes model is inconsistent with empirical observations; e.g., the volatility smile, skewness, and sudden large price fluctuations (intended as jumps in prices as done in [3]). Several models have been developed and studied in order to address these limitations and to ensure consistency with market data. Examples of these stochastic processes, but are not limited to, include local volatility models, stochastic volatility models, and (stochastic volatility) jump-diffusion models.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2412.10592v2 - Self-Exciting Random Evolutions (SEREs) and their Applications (Version 2)
- Date: 2024-12-13
- PDF: ./2412.10592v2_Self-Exciting_Random_Evolution.pdf
- Models/Methods: Hawkes Process, Markov-Modulated
- Evidence: Unclear
- Domain: General
- Keywords: self-exciting random evolutions (SEREs); self-walking imbed-
- Abstract: This paper is devoted to the study of a new class of random evol utions (RE), so-called self-exciting random evolutions (SE REs), and their applications. We also introduce a new random process x(t) such that it is based on a superposition of a Markov chain xn and a Hawkes process N (t), i.e., x(t) := xN (t). We call this process selfwalking imbedded semi-Hawkes process (Swish Process or Swi shP).
- Intro highlights: The purpose of the present paper is to introduce and to study a ne w class of Random Evolutions (RE), such as self-exciting RE (SERE), which a re 1The author thanks to two unanimous referees for their valuable co mments and remarks with respect to the rst version of this working paper (Availa ble at SSRN: https://ssrn.com/abstract=5055075). The work on this working paper is still in progress. 1 operator-valued stochastic processes that possess two traits , self-exciting and clustering ones.
- Contributions: This paper is devoted to the study of a new class of random evol utions (RE), so-called self-exciting random evolutions (SE REs), and their applications. We also introduce a new random process x(t) such that it is based on a superposition of a Markov chain xn and a Hawkes process N (t), i.e., x(t) := xN (t). We call this process selfwalking imbedded semi-Hawkes process (Swish Process or Swi shP). In this paper we have developed a new class of so-called self-exciting random evolutions (SEREs) and their applications.
- Conclusion: In this paper we have developed a new class of so-called self-exciting random evolutions (SEREs) and their applications. We have rst introduced a new random process x(t) such that it is based on a superposition of a Markov chain xn and a Hawkes process N(t), i.e., x(t) := xN (t). We call this process self-walking imbedded semi-Hawkes process (Swish Process or SwishP).
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2411.04616v1 - Optimal Execution under Incomplete Information
- Date: 2024-11-07
- PDF: ./2411.04616v1_Optimal_Execution_under_Incomp.pdf
- Models/Methods: Hawkes Process, Marked Hawkes, Price Impact, Impulse Control, Optimal Execution
- Evidence: Unclear
- Domain: LOB, Execution, Volatility, Order Flow
- Keywords: Optimal Execution, Impulse Control, Stochastic Filtering, Hawkes Processes, Market
- Abstract: We study optimal liquidation strategies under partial information for a single asset within a finite time horizon. We propose a model tailored for high-frequency trading, capturing price formation driven solely by order flow through mutually stimulating marked Hawkes processes. The model assumes a limit order book framework, accounting for both permanent price impact and transient market impact.
- Intro highlights: In modern financial markets, the execution of large orders within short timeframes presents unique challenges. Traders must develop strategies to maximize profits while minimizing risks, navigating a complex landscape influenced by immediate market depth and liquidity constraints. The studies by Bouchaud, Farmer, and Lillo [13], Zhou [49], and Taranto et al.
- Contributions: We study optimal liquidation strategies under partial information for a single asset within a finite time horizon. We propose a model tailored for high-frequency trading, capturing price formation driven solely by order flow through mutually stimulating marked Hawkes processes.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2410.08744v3 - No Tick-Size Too Small: A General Method for Modelling Small Tick Limit Order Books
- Date: 2024-10-11
- PDF: ./2410.08744v3_No_Tick-Size_Too_Small:_A_Gene.pdf
- Models/Methods: None detected
- Evidence: Unclear
- Domain: LOB
- Keywords: Limit Order Book; Microstructure; Tick-Sizes; Simulation; Stylized Facts; Liquidity;
- Abstract: Tick-sizes not only influence the granularity of the price formation process but also affect market agents' behavior. We investigate the disparity in the microstructural properties of the Limit Order Book (LOB) across a basket of assets with different relative tick-sizes. A key contribution of this ...
- Intro highlights: The rapid electronication of the nancial market has made the data structure Limit Order Books the central domain of essentially all trading, particularly for equities. Limit Order Books match sellers with the buyers according to their price priority rst, and queue priority second. These markets are classied as order driven instead of quote driven since there exists a lit, or in other words publicly visible, list of unmatched orders that any market participant can utilize for their trading needs.
- Contributions: We investigate the disparity in the microstructural properties of the Limit Order Book (LOB) across a basket of assets with different relative tick-sizes.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2410.08420v1 - Variance-Hawkes Process and its Application to Energy Markets
- Date: 2024-10-10
- PDF: ./2410.08420v1_Variance-Hawkes_Process_and_it.pdf
- Models/Methods: Hawkes Process
- Evidence: Unclear
- Domain: Volatility, Energy, Futures
- Keywords: variance-Hawkes process, generator, Ito formula, WTI crude oil, NYMEX
- Abstract: We define a new model using a Hawkes process as a subordinator in a standard Brownian motion. We demonstrate that this Hawkes subordinated Brownian motion or more succinctly, variance-Hawkes process can be fit to 2018 and 2019 natural gas and crude oil front-month futures log returns. This variance-Hawkes process allows financial models to easily have clustering effects encoded into their behaviour in a simple and tractable way.
- Intro highlights: Commodity spot prices have many unique and interesting properties that need to be reliably implemented in order to achieve an effective model. Some of these properties include volatility clustering, stochastic volatility, high kurtosis, volatility smile, jumps, 1 arXiv:2410.08420v1 [q-fin.MF] 10 Oct 2024 and mean-reversion. Our aim in this paper is to directly address the first four properties in a novel and flexible way which allows researchers to easily add them to their models.
- Contributions: We define a new model using a Hawkes process as a subordinator in a standard Brownian motion. We demonstrate that this Hawkes subordinated Brownian motion or more succinctly, variance-Hawkes process can be fit to 2018 and 2019 natural gas and crude oil front-month futures log returns.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2408.03594v1 - Forecasting High Frequency Order Flow Imbalance
- Date: 2024-08-07
- PDF: ./2408.03594v1_Forecasting_High_Frequency_Ord.pdf
- Models/Methods: Hawkes Process, Order Flow Imbalance
- Evidence: Unclear
- Domain: LOB, Order Flow
- Keywords: Market Microstructure, Order Flow, High Frequency Trading, Hawkes Process, Model
- Abstract: Market information events are generated intermittently and disseminated at high speeds in realtime. Market participants consume this high-frequency data to build limit order books, representing the current bids and offers for a given asset. The arrival processes, or the order flow of bid and offer events, are asymmetric and possibly dependent on each other.
- Intro highlights: Capital markets offer buyers and sellers a transparent and efficient mechanism to exchange goods for capital. In most modern capital markets, a continuous double auction mechanism is used where both the goods being offered for sale by a seller at a price (called the Ask price) and the price that a buyer is willing to pay (the Bid price) is continuously matched. Whenever the best ask price (the minimum price a seller is willing to accept) matches the best bid price (the maximum price a buyer is willing to pay), a trade is said to have occurred, and the goods offered by the seller are exchanged for the capital bid by the buyer.
- Contributions: Unlike traditional classification algorithms, such as the Lee-Ready algorithm and bulk trade classification, which can falter in highly volatile markets, this paper presents a novel method for computing high-frequency OFI without relying on these classifications.
- Conclusion: Our study employs Hawkes processes and Vector Auto Regression to construct an Order Flow Imbalance indicator, capturing the interdependence between BUY and SELL trades. By advancing multiple forecasting models and establishing a general framework for computing the loss function, this study provides a robust method for identifying a benchmark forecasting model among various competing models. Unlike traditional classification algorithms, such as the Lee-Ready algorithm and bulk trade classification, which can falter in highly volatile markets, this paper presents a novel method for computing high-frequency OFI without relying on these classifications.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2406.13508v1 - Pricing VIX options under the Heston-Hawkes stochastic volatility model
- Date: 2024-06-19
- PDF: ./2406.13508v1_Pricing_VIX_options_under_the_.pdf
- Models/Methods: Hawkes Process, Heston-Hawkes, Option Pricing
- Evidence: Unclear
- Domain: Options/VIX, Volatility
- Keywords: VIX options, Volatility with self-exciting jumps, Hawkes p rocess, Option pric-
- Abstract: We derive a semi-analytical pricing formula for European VI X call options under the Heston-Hawkes stochastic volatility model introduced in [ 7]. This arbitrage-free model incorporates the volatility clustering feature by adding an inde pendent compound Hawkes process to the Heston volatility. Using the Markov property of the ex ponential Hawkes an explicit expression of VIX 2 is derived as a linear combination of the variance and the Haw kes intensity.
- Intro highlights: Volatility trading has become remarkably important in nan ce in recent years and plays a crucial role in risk management, portfolio diversication, asset p ricing and econometrics. The Chicago Board Options Exchange (CBOE) established the rst volatil ity index, the VXO, in January 1993. According to [ 35], the VXO index represents the implied volatility of a hypot hetical at-the-money option on the S&P 100 (OEX) expiring in 30 days.
- Contributions: We derive a semi-analytical pricing formula for European VI X call options under the Heston-Hawkes stochastic volatility model introduced in [ 7].
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2405.03496v3 - Price-Aware Automated Market Makers: Models Beyond Brownian Prices and Static Liquidity
- Date: 2024-05-06
- PDF: ./2405.03496v3_Price-Aware_Automated_Market_M.pdf
- Models/Methods: Hawkes Process, Market Making
- Evidence: Unclear
- Domain: Market Making, Volatility
- Keywords: AMM, DeFi, stochastic optimal control, Heston-Bates model , Stein-Stein model, Hawkes
- Abstract: In this paper, we introduce a suite of models for price-aware automated market making platforms willing to optimize their quotes. These models incorporate advanced price dynamics, including stochastic volatility, jumps, and microstructural price models based on Hawkes processes. Additionally, we address the variability in demand from liquidity takers through mod els that employ either Hawkes or Markovmodulated Poisson processes.
- Intro highlights: Market makers, including both human agents and algorithms, are key participants in nancial markets standing ready to buy and sell securities or currency pairs. They a re crucial in facilitating transactions by bridging the gap between buyers and sellers, whose requests or orders may not coincide due to asynchronous submissions. The challenges faced by market makers, and the broader micro economics of market making, have been rigorously examined since the 1980s from two perspectives.
- Contributions: In this paper, we introduce a suite of models for price-aware automated market making platforms willing to optimize their quotes. Additionally, we address the variability in demand from liquidity takers through mod els that employ either Hawkes or Markovmodulated Poisson processes. In this paper, we introduced a comprehensive suite of models tailored for price-aware automated market makers designed to optimize their quoting strategies.
- Conclusion: In this paper, we introduced a comprehensive suite of models tailored for price-aware automated market makers designed to optimize their quoting strategies. Our explora tion spanned a variety of advanced price dynamics, including stochastic volatility, price jumps, and microst ructural models based on Hawkes processes. However, the adoption of these sophisticated models brings challeng es, particularly in terms of higher computational costs, which must be considered as they can restrict practic al application.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2401.11495v3 - Functional Limit Theorems for Hawkes Processes
- Date: 2024-01-21
- PDF: ./2401.11495v3_Functional_Limit_Theorems_for_.pdf
- Models/Methods: Hawkes Process
- Evidence: Theory
- Domain: General
- Keywords: and phrases: Hawkes process, functional limit theorem, regular variation, conv ergence rate.
- Abstract: We prove that the long-run behavior of Hawkes processes is fully de termined by the average number and the dispersion of child events. For subcritical processes we provid e FLLNs and FCLTs under minimal conditions on the kernel of the process with the precise form of th e limit theorems depending strongly on the dispersion of child events. For a critical Hawkes process with weakly dispersed child events, functional central limit theorems do not hold.
- Intro highlights: A Hawkes process N := {N (t) : t  0} is a random point process that models self-exciting arrival s of random events. Its intensity  := {(t) : t  0} is usually of the form (t) :=  (t) +  0<i<t (t  i) =  (t) +  (0,t ) (t  s)N (ds), (1.1) for some immigration density   L1 loc(R+; R+) that captures the immigration of exogenous events, and some kernel   L1(R+; R+) that captures the self-exciting impact of past events on th e arrivals of future events. The random variable i denotes the arrival time of the i-th event, for each i  N.
- Contributions: We prove that the long-run behavior of Hawkes processes is fully de termined by the average number and the dispersion of child events. For subcritical processes we provid e FLLNs and FCLTs under minimal conditions on the kernel of the process with the precise form of th e limit theorems depending strongly on the dispersion of child events.
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2401.09361v3 - Neural Hawkes: Non-Parametric Estimation in High Dimension and Causality Analysis in Cryptocurrency Markets
- Date: 2024-01-17
- PDF: ./2401.09361v3_Neural_Hawkes:_Non-Parametric_.pdf
- Models/Methods: Hawkes Process, Neural Hawkes, Marked Hawkes
- Evidence: Empirical, Simulation
- Domain: Volatility, Crypto
- Keywords: Hawkes process, Non-parametric estimation, Physics-informed neural network, Cryp-
- Abstract: We propose a novel approach to marked Hawkes kernel inference which we name the moment-based neural Hawkes estimation method. Hawkes processes are fully characterized by their first and second order statistics through a Fredholm integral equation of the second kind. Using recent advances in solving partial differential equations with physics-informed neural networks, we provide a numerical procedure to solve this integral equation in high dimension.
- Intro highlights: 2 1.1 Literature review of non-parametric estimation methods . . .
- Contributions: We propose a novel approach to marked Hawkes kernel inference which we name the moment-based neural Hawkes estimation method. Using recent advances in solving partial differential equations with physics-informed neural networks, we provide a numerical procedure to solve this integral equation in high dimension.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2312.08927v5 - Limit Order Book Dynamics and Order Size Modelling Using Compound Hawkes Process
- Date: 2023-12-14
- PDF: ./2312.08927v5_Limit_Order_Book_Dynamics_and_.pdf
- Models/Methods: Hawkes Process
- Evidence: Unclear
- Domain: LOB, Volatility, Order Flow
- Abstract: Hawkes Process has been used to model Limit Order Book (LOB) dynamics in several ways in the literature however the focus has been limited to capturing the inter-event times while the order size is usually assumed to be constant. We propose a novel methodology of using Compound Hawkes Process for the LOB where each event has an order size sampled from a calibrated distribution. The process is formulated in a novel way such that the spread of the process always remains positive.
- Intro highlights: The Hawkes process, known for its high adaptability, offers a more comprehensive point process methodology for modeling order book arrivals than the Poisson process and its variants, without the need to explicitly model individual traders behaviors in the market. Its capability to replicate microstructural details such as volatility clustering and correlated order flow makes it a suitable candidate for Limit Order Book (LOB) models. It is important to highlight that these point process models are mathematically descriptive, providing full transparency in their nature and thus are suitable for applications where black-box solutions are not preferred.
- Contributions: We propose a novel methodology of using Compound Hawkes Process for the LOB where each event has an order size sampled from a calibrated distribution.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2312.08784v2 - Convergence of Heavy-Tailed Hawkes Processes and the Microstructure of Rough Volatility
- Date: 2023-12-14
- PDF: ./2312.08784v2_Convergence_of_Heavy-Tailed_Ha.pdf
- Models/Methods: Hawkes Process, Rough Heston, Rough Volatility
- Evidence: Unclear
- Domain: Volatility
- Abstract: We establish the weak convergence of the intensity of a nearly-uns table Hawkes process with heavy-tailed kernel. Our result is used to derive a scaling limit for a na ncial market model where orders to buy or sell an asset arrive according to a Hawkes p rocess with power-law kernel. After suitable rescaling the price-volatility process converges wea kly to a rough Heston model.
- Intro highlights: and overview First introduced by Hawkes in [ 34, 35] to model cross-dependencies between earthquakes and their aftershocks, Hawkes processes have long become a powe rful tool to model a variety of phenomena in the sciences, humanities, economics and nance. A Hawkes process is a random point process {N (t) : t  0} that models self-exciting arrivals of random events. In such settings, events arrive at random p oints in time 1 <  2 <  3 <    according to an intensity process {V (t) : t  0} that is usually of the form V (t) :=  (t) +  0<i<t (t  i) =  (t) +  (0,t ) (t  s)N (ds), t  0, (1.1) We thank Peter Bank and Masaaki Fukasawa for valuable feedba ck.
- Contributions: We establish the weak convergence of the intensity of a nearly-uns table Hawkes process with heavy-tailed kernel.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2310.09273v1 - Uncovering Market Disorder and Liquidity Trends Detection
- Date: 2023-10-13
- PDF: ./2310.09273v1_Uncovering_Market_Disorder_and.pdf
- Models/Methods: Price Impact, Optimal Execution
- Evidence: Unclear
- Domain: LOB, Execution
- Keywords: Liquidity Risk, Quickest Detection, Change-point Detection, Minimax Optimality,
- Abstract: The primary objective of this paper is to conceive and develop a new methodology to detect notable changes in liquidity within an order-driven market. We study a market liquidity model which allows us to dynamically quantify the level of liquidity of a traded asset using its limit order book data. The proposed metric holds potential for enhancing the aggressiveness of optimal execution algorithms, minimizing market impact and transaction costs, and serving as a reliable indicator of market liquidity for market makers.
- Intro highlights: Assets liquidity is an important factor in ensuring the efficient functioning of a market. Glosten and Harris [1] define liquidity as the ability of an asset to be traded rapidly, in significant volumes and with minimal price impact. Measuring liquidity, therefore, involves three aspects of the trading process: time, volume and price.
- Contributions: The primary objective of this paper is to conceive and develop a new methodology to detect notable changes in liquidity within an order-driven market. We study a market liquidity model which allows us to dynamically quantify the level of liquidity of a traded asset using its limit order book data.
- Conclusion: that the elements V 1 k k0 ,  V 2 k k0 , . . .
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2309.02994v1 - An Offline Learning Approach to Propagator Models
- Date: 2023-09-06
- PDF: ./2309.02994v1_An_Offline_Learning_Approach_t.pdf
- Models/Methods: Propagator Model, Price Impact, Optimal Execution
- Evidence: Empirical
- Domain: Execution
- Keywords: optimal portfolio liquidation, price impact, propagator m odels, predictive signals, Volterra
- Abstract: We consider an oine learning problem for an agent who rst es timates an unknown price impact kernel from a static dataset, and then designs strate gies to liquidate a risky asset while creating transient price impact. We propose a novel approac h for a nonparametric estimation of the propagator from a dataset containing correlated pric e trajectories, trading signals and metaorders. We quantify the accuracy of the estimated propa gator using a metric which depends explicitly on the dataset.
- Intro highlights: Price impact refers to the empirical fact that execution of a large order aects the risky assets price in an adverse and persistent manner and is leading to le ss favourable prices for the trader. Accurate estimation of transactions price impact is instr umental for designing protable trading strategies. Propagator models serve as a central tool in des cribing these phenomena mathematically (see Bouchaud et al.
- Contributions: We consider an oine learning problem for an agent who rst es timates an unknown price impact kernel from a static dataset, and then designs strate gies to liquidate a risky asset while creating transient price impact. We propose a novel approac h for a nonparametric estimation of the propagator from a dataset containing correlated pric e trajectories, trading signals and metaorders. We quantify the accuracy of the estimated propa gator using a metric which depends explicitly on the dataset.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2308.12179v1 - Investigating Short-Term Dynamics in Green Bond Markets
- Date: 2023-08-23
- PDF: ./2308.12179v1_Investigating_Short-Term_Dynam.pdf
- Models/Methods: Hawkes Process
- Evidence: Unclear
- Domain: Volatility, Energy
- Keywords: Green Bond, Jumps, Self-exciting
- Abstract: The paper investigates the effect of the label green in bond markets from the lens of the trading activity. The idea is that jumps in the dynamics of returns have a specific memory nature that can be well represented through a self-exciting process. Specifically, using Hawkes processes where the intensity is described through a continuous time moving average model, we study the highfrequency dynamics of bond prices.
- Intro highlights: In recent years the pressure of solutions for the environment (e.g., transition to a lower-carbon economy, energy efficiency, and construction of renewable source plants) has solicited the need of alternative funding. A clear example is the creation of a financial instrument called green bond, which is used exclusively to raise money for financing environmental (labeled as green) projects. Following the principles of the International Capital Markets Association (ICMA) some categories of these investments funded through green bonds are destined to renewable energy, green buildings, public transport, energy efficiency, water, and waste management.
- Contributions: Specifically, using Hawkes processes where the intensity is described through a continuous time moving average model, we study the highfrequency dynamics of bond prices.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2307.09077v1 - Estimation of an Order Book Dependent Hawkes Process for Large Datasets
- Date: 2023-07-18
- PDF: ./2307.09077v1_Estimation_of_an_Order_Book_De.pdf
- Models/Methods: Hawkes Process
- Evidence: Empirical
- Domain: General
- Abstract: A point process for event arrivals in high frequency trading is presented. The intensity is the product of a Hawkes process and high dimensional functions of covariates derived from the order book. Conditions for stationarity of the process are stated.
- Intro highlights: This paper presents an intensity model for event arrivals in high frequency trading. The intensity depends on order book information. The model is a Hawkes process where the intensity does not only depend on the time from an event arrival but also on the order book.
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2301.05157v2 - Statistical Learning with Sublinear Regret of Propagator Models
- Date: 2023-01-12
- PDF: ./2301.05157v2_Statistical_Learning_with_Subl.pdf
- Models/Methods: Propagator Model, Price Impact, Optimal Execution
- Evidence: Unclear
- Domain: Execution
- Keywords: optimal portfolio liquidation, price impact, propagator models, predic-
- Abstract: We consider a class of learning problems in which an agent liquidates a risky asset while creating both transient price impact driven by an unknown convolution propagator and linear temporary price impact with an unknown parameter. We characterize the traders performance as maximization of a revenue-risk functional, where the trader also exploits available information on a price predicting signal. We present a trading algorithm that alternates between exploration and exploitation phases and achieves sublinear regrets with highprobability.
- Intro highlights: 3 2 Problem formulation and main results 7 2.1 Episodic learning for optimal liquidation problems . . .
- Contributions: We consider a class of learning problems in which an agent liquidates a risky asset while creating both transient price impact driven by an unknown convolution propagator and linear temporary price impact with an unknown parameter. We characterize the traders performance as maximization of a revenue-risk functional, where the trader also exploits available information on a price predicting signal. We present a trading algorithm that alternates between exploration and exploitation phases and achieves sublinear regrets with highprobability.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2210.15343v1 - Change of measure in a Heston-Hawkes stochastic volatility model
- Date: 2022-10-27
- PDF: ./2210.15343v1_Change_of_measure_in_a_Heston-.pdf
- Models/Methods: Hawkes Process, Heston-Hawkes
- Evidence: Unclear
- Domain: Volatility
- Keywords: stochastic volatility, change of measure, risk neutral mea sure, existence of
- Abstract: We consider the stochastic volatility model obtained by add ing a compound Hawkes process to the volatility of the well-known Heston model. A Hawk es process is a self-exciting counting process with many applications in mathematical n ance, insurance, epidemiology, seismology and other elds. We prove a general result on the e xistence of a family of equivalent (local) martingale measures.
- Intro highlights: Valuation of assets and nancial derivatives constitutes o ne of the core subjects of modern nancial mathematics. There have been several approaches to asset pr icing all of which can be classied in two larger groups: equilibrium pricing and rational pricin g. The latter gives rise to the commonly used methodology of pricing nancial instruments by ruling out arbitrage opportunities.
- Contributions: We consider the stochastic volatility model obtained by add ing a compound Hawkes process to the volatility of the well-known Heston model. We prove a general result on the e xistence of a family of equivalent (local) martingale measures.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2209.07621v1 - Multivariate Hawkes-based Models in LOB: European, Spread and Basket Option Pricing
- Date: 2022-09-15
- PDF: ./2209.07621v1_Multivariate_Hawkes-based_Mode.pdf
- Models/Methods: Hawkes Process, Multivariate Hawkes, Option Pricing
- Evidence: Theory
- Domain: LOB, Options/VIX
- Keywords: Multivariate general compound Hawkes process (MGCHP); exponential
- Abstract: In this paper, we consider pricing of European options and spread options for Hawkes-based model for the limit order book. We introduce multivariate Hawkes process and the multivariable general compound Hawkes process. Exponential multivariate general compound Hawkes processes and limit theorems for them, namely, LLN and FCLT, are considered then.
- Intro highlights: Pricing options for the limit order book (LOB) was initiated very recently. For example, paper Remillard et al. (2019) prices European options in a discrete time model for the LOB, and hence builds a discrete time model for the structure of the limit order book, so that the price per share depends on the size of the transaction.
- Contributions: In this paper, we consider pricing of European options and spread options for Hawkes-based model for the limit order book. We introduce multivariate Hawkes process and the multivariable general compound Hawkes process. In this paper, we considered pricing of European options and spread options for Hawkesbased model for the limit order book. We introduced multivariate Hawkes process and the multivariable general compound Hawkes process.
- Conclusion: In this paper, we considered pricing of European options and spread options for Hawkesbased model for the limit order book. We introduced multivariate Hawkes process and the multivariable general compound Hawkes process. Exponential multivariate general compound Hawkes processes and limit theorems for them, namely, LLN and FCLT, have been considered then.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2207.09951v1 - Deep Reinforcement Learning for Market Making Under a Hawkes Process-Based Limit Order Book Model
- Date: 2022-07-20
- PDF: ./2207.09951v1_Deep_Reinforcement_Learning_fo.pdf
- Models/Methods: Hawkes Process, Multivariate Hawkes, Order Book Simulator, Market Making, Reinforcement Learning
- Evidence: Simulation
- Domain: LOB, Market Making
- Keywords: Finance, neural networks, stochastic opti-
- Abstract: The stochastic control problem of optimal market making is among the central problems in quantitative nance. In this paper, a deep reinforcement learningbased controller is trained on a weakly consistent, multivariate Hawkes process-based limit order book simulator to obtain market making controls. The proposed approach leverages the advantages of Monte Carlo backtesting and contributes to the line of research on market making under weakly consistent limit order book models.
- Contributions: In this paper, a deep reinforcement learningbased controller is trained on a weakly consistent, multivariate Hawkes process-based limit order book simulator to obtain market making controls.
- Conclusion: A DRL-based approach was used to obtain market-making strategies with superior performance, as compared to heuristic benchmarks. The approach yields promising results when realistic LOB simulators based on multivariate Hawkes processes are employed. Special focus was placed on the statistical analysis of the resulting PnL and terminal inventory distributions, and sensitivity analysis, both to changes in the underlying order intensity rates and the limit order fees.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2206.10419v3 - Multivariate Quadratic Hawkes Processes -- Part I: Theoretical Analysis
- Date: 2022-06-21
- PDF: ./2206.10419v3_Multivariate_Quadratic_Hawkes_.pdf
- Models/Methods: Hawkes Process, Quadratic Hawkes
- Evidence: Unclear
- Domain: Volatility
- Keywords: Multivariate QHawkes
- Abstract: Quadratic Hawkes (QHawkes) processes have proved effective at reproducing the statistics of price changes, capturing many of the stylised facts of financial markets. Motivated by the recently reported strong occurrence of endogenous co-jumps (simultaneous price jumps of several assets) we extend QHa...
- Intro highlights: Modelling the volatility of nancial assets is a significant challenge for academics, market participants and regulators alike. In fact, models describing the statistics of price changes are widely used for, e.g., risk control and derivative pricing. When not in line with the behaviour of real markets, these models can lead to disappointing outcomes, or even major mishaps.
- Contributions: Motivated by the recently reported strong occurrence of endogenous co-jumps (simultaneous price jumps of several assets) we extend QHa...
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2205.06338v1 - A Multivariate Hawkes Process Model for Stablecoin-Cryptocurrency Depegging Event Dynamics
- Date: 2022-05-12
- PDF: ./2205.06338v1_A_Multivariate_Hawkes_Process_.pdf
- Models/Methods: Hawkes Process, Multivariate Hawkes
- Evidence: Simulation
- Domain: Options/VIX, Crypto
- Abstract: Stablecoinsdigital assets pegged to a specic currency or commodity valueare heavily involved in transactions of major cryptocurrencies [1]. The eects of deviations from their desired xed values (depeggings) on the cryptocurrencies for which they are frequently used in transactions are therefore of interest to study. We propose a model for this phenomenon using a multivariate mutually-exciting Hawkes process, and present a numerical example applying this model to Tether (USDT) and Bitcoin (BTC).
- Intro highlights: 1.1 Stablecoins and Cryptocurrency The rise of digital marketswhether with respect to the digitalization of equity markets in the early 2010s or the increasing adoption of digital assets in the early 2020salways brings with it new dynamics that are important for market participants to identify and understand. Stablecoins are digital assets, predominantly supported by public blockchain networks, which are designed with the purpose of maintaining a stable value relative to a reference asset. These reference assets are usually national currencies or commodities.
- Contributions: We propose a model for this phenomenon using a multivariate mutually-exciting Hawkes process, and present a numerical example applying this model to Tether (USDT) and Bitcoin (BTC).
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2201.10173v1 - Modeling bid and ask price dynamics with an extended Hawkes process and its empirical applications for high-frequency stock market data
- Date: 2022-01-25
- PDF: ./2201.10173v1_Modeling_bid_and_ask_price_dyn.pdf
- Models/Methods: Hawkes Process
- Evidence: Empirical
- Domain: General
- Abstract: This study proposes a versatile model for the dynamics of the best bid and ask prices using an extended Hawkes process. The model incorporates the zero intensities of the spreadnarrowing processes at the minimum bid-ask spread, spread-dependent intensities, possible negative excitement, and nonnegative intensities. We apply the model to high-frequency best bid and ask price data from US stock markets.
- Intro highlights: A considerable volume of research on high-frequency trading, quotes, and nancial data is available. High-frequency quotes and trading, which are the main sources of high-frequency nancial data, are considered to be (automated) trading generally based on a (mathematical) algorithm that can generate a large number of quotes and trades over a short time horizon. However, the precise denition varies across studies and regulatory entities.
- Contributions: We apply the model to high-frequency best bid and ask price data from US stock markets.
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2112.14161v3 - On Hawkes Processes with Infinite Mean Intensity
- Date: 2021-12-28
- PDF: ./2112.14161v3_On_Hawkes_Processes_with_Infin.pdf
- Models/Methods: Hawkes Process, Quadratic Hawkes
- Evidence: Unclear
- Domain: General
- Keywords: Hawkes processes, Endogeneity ratio, Stationarity, QHawkes, ZHawkes
- Abstract: The stability condition for Hawkes processes and their non-linear extensions usually relies on the condition that the mean intensity is a finite constant. It follows that the total endogeneity ratio needs to be strictly smaller than unity. In the present note we argue that it is possible to have a t...
- Intro highlights: Hawkes processes have been used in various elds to model endogenous dynamics, where past activity triggers more activity in the future. Indeed, Hawkes processes were found to be relevant to capture the selfexcited nature of the dynamics in biological neural networks [1, 2], in nancial markets [3, 4], in seismologic activity (earthquakes) [5], and also in crime rates or riot propagation [6, 7]. Standard linear Hawkes processes are basically akin to a branching process, where each event generates on averagenH child events.
- Contributions: In the present note we argue that it is possible to have a t...
- Conclusion: is not always warranted. C. ZHawkes As an interesting special case that captures the Zumbach eect (i.e.
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2112.04245v1 - Do fundamentals shape the price response? A critical assessment of linear impact models
- Date: 2021-12-08
- PDF: ./2112.04245v1_Do_fundamentals_shape_the_pric.pdf
- Models/Methods: Propagator Model, Price Impact
- Evidence: Unclear
- Domain: Volatility
- Keywords: Market microstructure, price impact, calibration, multi-scale analysis.
- Abstract: We compare the predictions of the stationary Kyle model, a microfounded multi-step linear price impact model in which market prices forecast fundamentals through information encoded in the order ow, with those of the propagator model, a purely data-driven model in which trades mechanically impact prices with a time-decaying kernel. We nd that, remarkably, both models predict the exact same price dynamics at high frequency, due to the emergence of universality at small time scales. On the other hand, we nd those models to disagree on the overall strength of the impact function by a quantity that we are able to relate to the amount of excess-volatility in the market.
- Intro highlights: 3 2 Linear models for price impact 4 2.1 The stationary Kyle model . . .
- Contributions: We compare the predictions of the stationary Kyle model, a microfounded multi-step linear price impact model in which market prices forecast fundamentals through information encoded in the order ow, with those of the propagator model, a purely data-driven model in which trades mechanically impact prices with a time-decaying kernel. We nd that, remarkably, both models predict the exact same price dynamics at high frequency, due to the emergence of universality at small time scales. On the other hand, we nd those models to disagree on the overall strength of the impact function by a quantity that we are able to relate to the amount of excess-volatility in the market.
- HFT relevance: Indirect: volatility or options modeling; useful for risk and scenario generation.

## 2110.09417v1 - Mean-Variance Portfolio Selection in Contagious Markets
- Date: 2021-10-18
- PDF: ./2110.09417v1_Mean-Variance_Portfolio_Select.pdf
- Models/Methods: Hawkes Process, Multivariate Hawkes
- Evidence: Empirical
- Domain: General
- Keywords: Ecient strategy; Hawkes process; Jump-diusion; Linear-q uadratic control; Optimal invest-
- Abstract: We consider a mean-variance portfolio selection problem in a nancial market with contagion risk. The risky assets follow a jump-diusion model, in which jumps are driven by a multivariate Hawkes process with mutual-excitation eect. The mutual-excitation feature of th e Hawkes process captures the contagion risk in the sense that each price jump of an asset increases t he likelihood of future jumps not only in the same asset but also in other assets.
- Intro highlights: Asset prices exhibit jumps, occasionally and persistently , in all nancial markets across the world, which has been well documented and empirically tested in the liter ature. Large price movements are unlikely to be observed under standard nancial models driven solely by Brownian motion(s), e.g., the Black-Scholes model. The most popular models incorporating jumps are the j ump-diusion models, with the jump part predominantly driven by a Poisson process or a more general L  evy process.
- Contributions: We consider a mean-variance portfolio selection problem in a nancial market with contagion risk.
- Conclusion: is guaranteed by the relationship between the two approaches. That is, the value function and the adjoint process are related as follow s: p(t) = 2 e2r(T  t)+g(t,(t)) X (t) = Vx(t, X (t),  (t)), q(t) =     c (t)Y (t) =     c (t)Vxx(t, X (t),  (t)), u l (t, z l) = X (t )Vl(t) + k i=1   ci(t)il(zl) ( Y (t ) + Vl(t) ) = Vx(t, X (t ) + (  c (t) (z))(l),  (t) + (l))  V x(t, X (t ),  (t)). Moreover, substituting p(t) = Vx(t, X (t),  (t)) into the adjoint equation ( 3.4) and matching the drift gives the relationship between the Hamiltonian and the valu e function as below: L c [Vx(t, X (t),  (t))] = H x(t, X (t),   c (t), p (t), q (t), u (t)).
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2110.07075v1 - General Compound Hawkes Processes for Mid-Price Prediction
- Date: 2021-10-13
- PDF: ./2110.07075v1_General_Compound_Hawkes_Proces.pdf
- Models/Methods: Hawkes Process
- Evidence: Unclear
- Domain: LOB, Futures
- Keywords: Limit order book, Hawkes process, Futures data, Diusive limit, Price prediction
- Abstract: High frequency nancial data is burdened by a level of randomness that is unavoidable and obfuscates the task of modelling. This idea is reected in the intraday evolution of limit orders book data for many nancial assets and suggests several justications for the use of stochastic models. For instance, the arbitrary distribution of inter arrival times and the subsequent dependence structure between consecutive book events.
- Intro highlights: World nancial exchanges are highly integrated and accessible to almost anyone in the world. The electronic limit order book has become a fundamental way to understand, interact with and engineer nancial markets. Limit order books are in fact the main driver and source of data for high-frequency traders[12].
- Contributions: Let us recall that the steady state probabilities of a simple 2 state Markov chain are estimated as follows: 18 M = [1 p p q 1 q ] and consequently, = { p p + q, q p + q } so we nally we see that the steady state probabilities, and hence the entire directional prediction depend on the Markov chain transition probability matrix that is estimated from the data.
- Conclusion: is that the direction of our prediction for any time in the future completely depends on the steady state probabilities of the Markov chain. The Hawkes process does not seem to be involved in the directional prediction. Let us recall that the steady state probabilities of a simple 2 state Markov chain are estimated as follows: 18 M = [1 p p q 1 q ] and consequently,  = { p p + q, q p + q } so we nally we see that the steady state probabilities, and hence the entire directional prediction depend on the Markov chain transition probability matrix that is estimated from the data.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2110.01523v2 - Exact asymptotic solutions to nonlinear Hawkes processes: a systematic classification of the steady-state solutions
- Date: 2021-10-04
- PDF: ./2110.01523v2_Exact_asymptotic_solutions_to_.pdf
- Models/Methods: Hawkes Process
- Evidence: Unclear
- Domain: General
- Abstract: Hawkes point processes are first-order non-Markovian stochastic models of intermittent bursty dynamics with applications to physical, seismic, epidemic, biological, financial, and social systems. While accounting for positive feedback loops that may lead to critical phenomena in complex systems, the...
- Intro highlights: Intermittent bursts are ubiquitously observed with temporal and spatial clustering characters in physical [1, 2], seismic [36], epidemic [7], nancial [810], and social systems [11, 12]. Such bursty dynamics can be well described by the Hawkes process [1315], a non-Markovian self-excited point process capturing both long memory eects and critical bursts, such that past events keep their potential inuence to trigger future bursty events for a long time, potentially leading to critical bursts. However, the essential non-Markovian nature of this model has been an obstacle preventing the development of a unied analytical theory because the established framework of Markovian stochastic processes is not applicable.
- HFT relevance: Methods-focused: model family can inform simulation or calibration components.

## 2110.00771v2 - Non-average price impact in order-driven markets
- Date: 2021-10-02
- PDF: ./2110.00771v2_Non-average_price_impact_in_or.pdf
- Models/Methods: Price Impact
- Evidence: Unclear
- Domain: Execution
- Abstract: We present a measurement of price impact in order-driven markets that does not require averages across executions or scenarios. Given the order book data associated with one single execution of a sell metaorder, we measure its contribution to price decrease during the trade.
- Intro highlights: Price impact is the phenomenon whereby trade executions aect the price of the asset being traded. The price is aected in a way that is unfavourable to the trade, i.e. it decreases as a consequence of sell orders and it increases as a consequence of buy orders.
- Contributions: We present a measurement of price impact in order-driven markets that does not require averages across executions or scenarios. Given the order book data associated with one single execution of a sell metaorder, we measure its contribution to price decrease during the trade.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.

## 2109.15110v1 - Deep Hawkes Process for High-Frequency Market Making
- Date: 2021-09-30
- PDF: ./2109.15110v1_Deep_Hawkes_Process_for_High-F.pdf
- Models/Methods: Hawkes Process, Market Making
- Evidence: Simulation
- Domain: Market Making, Execution
- Abstract: High-frequency market making is a liquidity-providing trading strategy that simultaneously generates many bids and asks for a security at ultra-low latency while maintaining a relatively neutral position. The strategy makes a prot from the bid-ask spread for every buy and sell transaction, against the risk of adverse selection, uncertain execution and inventory risk. We design realistic simulations of limit order markets and develop a high-frequency market making strategy in which agents process order book information to post the optimal price, order type and execution time.
- Intro highlights: Technological innovations and regulatory initiatives in the nancial market have led to the traditional exchange oor being displaced by the electronic Copenhagen Business School, Denmark. Email:pk.mpp@cbs.dk 1 arXiv:2109.15110v1 [cs.CE] 30 Sep 2021 exchange. The electronic exchange is a fully automated trading system programmed to incisively enforce order precedence, pricing and the matching of buy and sell orders.
- Contributions: We design realistic simulations of limit order markets and develop a high-frequency market making strategy in which agents process order book information to post the optimal price, order type and execution time.
- HFT relevance: Direct: LOB/flow modeling or execution/market making signal design.
