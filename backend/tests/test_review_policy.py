import unittest
from copy import deepcopy
from audit_review_policy import review_source

class ReviewPolicyTests(unittest.TestCase):
    def sample(self, expected="8.3951328", actual="8.395", unit="т"):
        task=dict(w_vol=expected,w_unit=unit)
        data=dict(work_found=True,unit_confirmed=True,found_volume=actual,found_unit=unit,checks=[dict(claim_id="quantity",state="contradiction",reasons=["value_mismatch"],required=True),dict(claim_id="geometry:name:length:1",state="not_found",reasons=["no_proposal"],required=True)])
        return data,task
    def test_rounding_and_missing_name_dimension_are_not_conflicts(self):
        data,task=self.sample(); before=deepcopy(data)
        adjusted=review_source(data,task)
        self.assertEqual(adjusted["checks"][0]["state"],"confirmed")
        self.assertEqual(adjusted["checks"][1]["state"],"not_applicable")
        self.assertEqual(data,before)
    def test_real_difference_counts_and_unit_mismatch_are_preserved(self):
        for expected,actual,unit in [("8.3951328","8.3","т"),("114.04","114.0","шт"),("75","70","м")]:
            data,task=self.sample(expected,actual,unit)
            self.assertEqual(review_source(data,task)["checks"][0]["state"],"contradiction")
        data,task=self.sample();data["checks"][1]["state"]="contradiction"
        self.assertEqual(review_source(data,task)["checks"][1]["state"],"contradiction")
    def test_missing_comment_conditions_are_not_silently_confirmed(self):
        data,task=self.sample();data["checks"][1]["claim_id"]="geometry:comment:height:1"
        self.assertEqual(review_source(data,task)["checks"][1]["state"],"not_found")
