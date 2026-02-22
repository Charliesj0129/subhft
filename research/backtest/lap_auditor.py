"""
LAP Auditor (Paper 007)

Audits LLM Forecasts for Lookahead Bias using Lookahead Propensity (LAP).
Checks if the model uses "Recall" (Memory) instead of "Reasoning".
"""

import numpy as np
from dataclasses import dataclass
from typing import List

@dataclass
class LAPAuditResult:
    passed: bool
    correlation: float
    notes: str

class LAPAuditor:
    def __init__(self, threshold_corr: float = -0.1): 
        # Threshold for suspicious correlation between Error and LAP.
        # If Corr < -0.1 (Error drops as LAP increases), it's suspicious.
        self.threshold = threshold_corr
        
    def audit(self, abs_errors: List[float], lap_scores: List[float]) -> LAPAuditResult:
        """
        Auditforecasts.
        abs_errors: Absolute error of predictions |y_true - y_pred|.
        lap_scores: LAP score of the prompt (Mocked from LLM logprobs).
        """
        if len(abs_errors) != len(lap_scores) or len(abs_errors) < 10:
            return LAPAuditResult(True, 0.0, "Insufficient Data")
            
        e = np.array(abs_errors)
        l = np.array(lap_scores)
        
        # Pearson Correlation
        if np.std(e) == 0 or np.std(l) == 0:
            return LAPAuditResult(True, 0.0, "Constant Data")
            
        corr = np.corrcoef(e, l)[0, 1]
        
        passed = True
        notes = f"Correlation(Error, LAP) = {corr:.4f}. No significant bias detected."
        
        if corr < self.threshold:
            passed = False
            notes = f"FAIL: Significant Negative Correlation ({corr:.4f}). High LAP (Familiarity) leads to Low Error, suggesting Lookahead/Recall."
            
        return LAPAuditResult(passed, corr, notes)
