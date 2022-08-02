# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from typing import Any, Dict, List, Tuple

from bugbug.models.defect import DefectModel


class RegressionModel(DefectModel):
    def __init__(self, lemmatization=False, historical=False):
        DefectModel.__init__(self, lemmatization, historical)
        self.calculate_importance = False

    def get_labels(self) -> Tuple[Dict[int, Any], List[int]]:
        classes = self.get_bugbug_labels("regression")

        print(f"{sum(label == 1 for label in classes.values())} regression bugs")
        print(f"{sum(label == 0 for label in classes.values())} non-regression bugs")

        return classes, [0, 1]

    def overwrite_classes(self, bugs, classes, probabilities):
        for i, bug in enumerate(bugs):
            regression_keyword_removed = False
            for history in bug["history"]:
                for change in history["changes"]:
                    if change["field_name"] == "keywords":
                        if "regression" in [
                            k.strip() for k in change["removed"].split(",")
                        ]:
                            regression_keyword_removed = True
                        elif "regression" in [
                            k.strip() for k in change["added"].split(",")
                        ]:
                            regression_keyword_removed = False

            if regression_keyword_removed:
                classes[i] = [1.0, 0.0] if probabilities else 0

        super().overwrite_classes(bugs, classes, probabilities)

        return classes
