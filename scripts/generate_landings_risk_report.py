# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import collections
import itertools
import json
import logging
import math
import os
import re
import statistics
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Set, Tuple, cast

import dateutil.parser
import requests
import taskcluster
from dateutil.relativedelta import relativedelta
from tqdm import tqdm

from bugbug import bug_features, bugzilla, db, phabricator, repository, test_scheduling
from bugbug.models.bugtype import bug_to_types
from bugbug.models.regressor import BUG_FIXING_COMMITS_DB, RegressorModel
from bugbug.utils import (
    download_check_etag,
    download_model,
    get_secret,
    get_taskcluster_options,
    zstd_compress,
    zstd_decompress,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


TEST_INFOS_DB = "data/test_info.json"
db.register(
    TEST_INFOS_DB,
    "https://community-tc.services.mozilla.com/api/index/v1/task/project.bugbug.landings_risk_report.latest/artifacts/public/test_info.json.zst",
    2,
)

PAST_REGRESSIONS_BY_URL = "https://community-tc.services.mozilla.com/api/index/v1/task/project.bugbug.past_bugs_by_unit.latest/artifacts/public/past_regressions_by_{dimension}.json.zst"
PAST_FIXED_BUGS_BY_URL = "https://community-tc.services.mozilla.com/api/index/v1/task/project.bugbug.past_bugs_by_unit.latest/artifacts/public/past_fixed_bugs_by_{dimension}.json.zst"
PAST_REGRESSION_BLOCKED_BUGS_BY_URL = "https://community-tc.services.mozilla.com/api/index/v1/task/project.bugbug.past_bugs_by_unit.latest/artifacts/public/past_regression_blocked_bugs_by_{dimension}.json.zst"
PAST_FIXED_BUG_BLOCKED_BUGS_BY_URL = "https://community-tc.services.mozilla.com/api/index/v1/task/project.bugbug.past_bugs_by_unit.latest/artifacts/public/past_fixed_bug_blocked_bugs_by_{dimension}.json.zst"


FUZZING_METABUG_ID = 316898


def _deduplicate(bug_summaries: List[dict]) -> List[dict]:
    seen = set()
    results = []
    for bug_summary in bug_summaries[::-1]:
        if bug_summary["id"] in seen:
            continue
        seen.add(bug_summary["id"])

        results.append(bug_summary)

    return results[::-1]


def _download_past_bugs(url: str) -> dict:
    path = os.path.join("data", os.path.basename(url)[:-4])
    download_check_etag(url, path=f"{path}.zst")
    zstd_decompress(path)
    assert os.path.exists(path)
    with open(path, "r") as f:
        return json.load(f)


def parse_risk_band(risk_band: str) -> Tuple[str, float, float]:
    name, start, end = risk_band.split("-")
    return (name, float(start), float(end))


def is_fuzzblocker(bug: bugzilla.BugDict) -> bool:
    return "fuzzblocker" in bug["whiteboard"].lower()


def get_full_component(bug):
    return f'{bug["product"]}::{bug["component"]}'


def histogram(components: List[str]) -> Dict[str, float]:
    counter = collections.Counter(components)
    return {
        component: count / len(components) for component, count in counter.most_common()
    }


def component_histogram(bugs: List[dict]) -> Dict[str, float]:
    return histogram([bug["component"] for bug in bugs])


# TODO: Remove once the mapping is updated in Bugzilla.
def get_component_team_mapping() -> Dict[str, Dict[str, str]]:
    component_team_mapping = bugzilla.get_component_team_mapping()

    component_team_mapping_override = json.loads(
        get_secret("COMPONENT_TEAM_MAPPING_OVERRIDE")
    )
    for product, components_teams in component_team_mapping_override.items():
        for component, team in components_teams.items():
            component_team_mapping[product][component] = team

    return component_team_mapping


class LandingsRiskReportGenerator(object):
    def __init__(self, repo_dir: str) -> None:
        self.risk_bands = sorted(
            (
                parse_risk_band(risk_band)
                for risk_band in get_secret("REGRESSOR_RISK_BANDS").split(";")
            ),
            key=lambda x: x[1],
        )

        repository.clone(repo_dir)

        logger.info("Downloading commits database...")
        assert db.download(repository.COMMITS_DB, support_files_too=True)

        logger.info("Updating commits DB...")
        for commit in repository.get_commits():
            pass

        repository.download_commits(repo_dir, rev_start=f'children({commit["node"]})')

        # Some commits that were already in the DB from the previous run might need
        # to be updated (e.g. coverage information).
        repository.update_commits()

        logger.info("Downloading revisions database...")
        assert db.download(phabricator.REVISIONS_DB)

        logger.info("Downloading bugs database...")
        assert db.download(bugzilla.BUGS_DB)

        logger.info("Download commit classifications...")
        assert db.download(BUG_FIXING_COMMITS_DB)

        self.regressor_model = cast(
            RegressorModel, RegressorModel.load(download_model("regressor"))
        )

        bugzilla.set_token(get_secret("BUGZILLA_TOKEN"))
        phabricator.set_api_key(
            get_secret("PHABRICATOR_URL"), get_secret("PHABRICATOR_TOKEN")
        )

        self.path_to_component = repository.get_component_mapping()

        self.past_regressions_by = {}
        self.past_fixed_bugs_by = {}
        self.past_regression_blocked_bugs_by = {}
        self.past_fixed_bug_blocked_bugs_by = {}

        for dimension in ["component", "directory", "file", "function"]:
            self.past_regressions_by[dimension] = _download_past_bugs(
                PAST_REGRESSIONS_BY_URL.format(dimension=dimension)
            )
            self.past_fixed_bugs_by[dimension] = _download_past_bugs(
                PAST_FIXED_BUGS_BY_URL.format(dimension=dimension)
            )
            self.past_regression_blocked_bugs_by[dimension] = _download_past_bugs(
                PAST_REGRESSION_BLOCKED_BUGS_BY_URL.format(dimension=dimension)
            )
            self.past_fixed_bug_blocked_bugs_by[dimension] = _download_past_bugs(
                PAST_FIXED_BUG_BLOCKED_BUGS_BY_URL.format(dimension=dimension)
            )

    def get_prev_bugs(
        self,
        past_bugs_by: dict,
        commit: repository.CommitDict,
        component: str = None,
    ) -> List[dict]:
        paths = [
            path
            for path in commit["files"]
            if component is None
            or (
                path.encode("utf-8") in self.path_to_component
                and self.path_to_component[path.encode("utf-8")]
                == component.encode("utf-8")
            )
        ]

        past_bugs = []

        for path, f_group in commit["functions"].items():
            if path not in paths:
                continue

            if path not in past_bugs_by["function"]:
                continue

            found = False
            for f in f_group:
                if f["name"] not in past_bugs_by["function"][path]:
                    continue

                found = True
                past_bugs += past_bugs_by["function"][path][f["name"]]

            if found:
                paths.remove(path)

        for path in paths:
            if path in past_bugs_by["file"]:
                past_bugs += past_bugs_by["file"][path]
                paths.remove(path)

        for path, directories in zip(paths, repository.get_directories(paths)):
            found = False
            for directory in directories:
                if directory in past_bugs_by["directory"]:
                    found = True
                    past_bugs += past_bugs_by["directory"][directory]

            if found:
                paths.remove(path)

        components = [
            self.path_to_component[path.encode("utf-8")].tobytes().decode("utf-8")
            for path in paths
            if path.encode("utf-8") in self.path_to_component
        ]

        for component in components:
            if component in past_bugs_by["component"]:
                past_bugs += past_bugs_by["component"][component]

        return past_bugs

    def get_prev_bugs_stats(
        self,
        commit_group: dict,
        commit_list: List[repository.CommitDict],
        component: str = None,
    ) -> None:
        # Find previous regressions occurred in the same files as those touched by these commits.
        # And find previous bugs that were fixed by touching the same files as these commits.
        # And find previous bugs that were blocked by regressions occurred in the same files as those touched by these commits.
        # And find previous bugs that were blocked by bugs that were fixed by touching the same files as those touched by these commits.
        prev_regressions: List[Dict[str, Any]] = sum(
            (
                self.get_prev_bugs(self.past_regressions_by, commit, component)
                for commit in commit_list
            ),
            [],
        )
        prev_fixed_bugs: List[Dict[str, Any]] = sum(
            (
                self.get_prev_bugs(self.past_fixed_bugs_by, commit, component)
                for commit in commit_list
            ),
            [],
        )
        prev_regression_blocked_bugs: List[Dict[str, Any]] = sum(
            (
                self.get_prev_bugs(
                    self.past_regression_blocked_bugs_by, commit, component
                )
                for commit in commit_list
            ),
            [],
        )
        prev_fixed_bug_blocked_bugs: List[Dict[str, Any]] = sum(
            (
                self.get_prev_bugs(
                    self.past_fixed_bug_blocked_bugs_by, commit, component
                )
                for commit in commit_list
            ),
            [],
        )

        prev_regressions = _deduplicate(prev_regressions)
        prev_fixed_bugs = _deduplicate(prev_fixed_bugs)
        prev_regression_blocked_bugs = _deduplicate(prev_regression_blocked_bugs)
        prev_fixed_bug_blocked_bugs = _deduplicate(prev_fixed_bug_blocked_bugs)

        regression_components = component_histogram(prev_regressions)
        fixed_bugs_components = component_histogram(prev_fixed_bugs)
        regression_blocked_bug_components = component_histogram(
            prev_regression_blocked_bugs
        )
        fixed_bug_blocked_bug_components = component_histogram(
            prev_fixed_bug_blocked_bugs
        )

        commit_group["most_common_regression_components"] = regression_components
        # These are only used for component connections for the time being.
        if component:
            commit_group["prev_regressions"] = prev_regressions[-3:]
            commit_group["prev_fixed_bugs"] = prev_fixed_bugs[-3:]
            commit_group["prev_regression_blocked_bugs"] = prev_regression_blocked_bugs[
                -3:
            ]
            commit_group["prev_fixed_bug_blocked_bugs"] = prev_fixed_bug_blocked_bugs[
                -3:
            ]
            commit_group["most_common_fixed_bugs_components"] = fixed_bugs_components
            commit_group[
                "most_common_regression_blocked_bug_components"
            ] = regression_blocked_bug_components
            commit_group[
                "most_common_fixed_bug_blocked_bug_components"
            ] = fixed_bug_blocked_bug_components

    def get_landed_and_filed_since(self, days: int) -> List[int]:
        since = datetime.utcnow() - timedelta(days=days)

        commits = []
        last_commit_by_bug: Dict[int, datetime] = {}
        for commit in repository.get_commits():
            if not commit["bug_id"]:
                continue

            push_date = dateutil.parser.parse(commit["pushdate"])

            if push_date >= since and (
                commit["bug_id"] not in last_commit_by_bug
                or push_date - last_commit_by_bug[commit["bug_id"]] < timedelta(days=91)
                or not all(repository.is_test(p) for p in commit["files"])
            ):
                commits.append(commit)

            last_commit_by_bug[commit["bug_id"]] = push_date

        logger.info(f"Retrieving bug IDs since {days} days ago")
        timespan_ids = bugzilla.get_ids_between(since, resolution=["---", "FIXED"])

        return list({commit["bug_id"] for commit in commits} | set(timespan_ids))

    def get_regressors_of(self, bug_ids: List[int]) -> List[int]:
        bugzilla.download_bugs(bug_ids)
        return sum(
            (
                bug["regressed_by"]
                for bug in bugzilla.get_bugs()
                if bug["id"] in bug_ids
            ),
            [],
        )

    def get_blocking_of(
        self, bug_ids: List[int], meta_only: bool = False
    ) -> Dict[int, List[int]]:
        bug_map = {bug["id"]: bug for bug in bugzilla.get_bugs()}
        return {
            bug_id: bugzilla.find_blocking(bug_map, bug_map[bug_id])
            for bug_id in bug_ids
            if not meta_only or "meta" in bug_map[bug_id]["keywords"]
        }

    def get_meta_bugs(self, days: int) -> List[int]:
        return bugzilla.get_ids(
            {
                "keywords": "feature-testing-meta",
                "keywords_type": "allwords",
                "resolution": "---",
                "f1": "anything",
                "o1": "changedafter",
                "v1": "-90d",
            }
        )

    def retrieve_test_info(self, days: int) -> Dict[str, Any]:
        logger.info("Download previous test info...")
        db.download(TEST_INFOS_DB)

        dates = [
            datetime.utcnow() - timedelta(days=day) for day in reversed(range(days))
        ]

        logger.info("Get previously gathered test info...")
        test_infos = {
            test_info["date"]: test_info for test_info in db.read(TEST_INFOS_DB)
        }

        prev_skips = None
        for date in tqdm(dates):
            date_str = date.strftime("%Y-%m-%d")

            # Gather the latest three days again, as the data might have changed.
            if date_str in test_infos and date < datetime.utcnow() - timedelta(days=3):
                prev_skips = test_infos[date_str]["skips"]
                continue

            test_infos[date_str] = {
                "date": date_str,
                "bugs": [
                    {"id": item["bug_id"], "count": item["bug_count"]}
                    for item in test_scheduling.get_failure_bugs(date, date)
                ],
                "skips": {},
            }

            try:
                test_info = test_scheduling.get_test_info(date)

                for component in test_info["tests"].keys():
                    test_infos[date_str]["skips"][component] = sum(
                        "skip-if" in test for test in test_info["tests"][component]
                    )

            except requests.exceptions.HTTPError:
                # If we couldn't find a test info artifact for the given date, assume the number of skip-ifs didn't change from the previous day.
                assert prev_skips is not None
                test_infos[date_str]["skips"] = prev_skips

            prev_skips = test_infos[date_str]["skips"]

        db.write(
            TEST_INFOS_DB,
            (
                test_infos[date.strftime("%Y-%m-%d")]
                for date in dates
                if date.strftime("%Y-%m-%d") in test_infos
            ),
        )
        zstd_compress(TEST_INFOS_DB)

        return test_infos

    def generate_landings_by_date(
        self,
        bug_map: Dict[int, bugzilla.BugDict],
        regressor_bug_ids: Set[int],
        bugs: List[int],
        meta_bugs: Dict[int, List[int]],
    ) -> None:
        # A map from bug ID to the list of commits associated to the bug (in order of landing).
        bug_to_commits = collections.defaultdict(list)

        for commit in repository.get_commits():
            bug_id = commit["bug_id"]
            if not bug_id:
                continue

            if bug_id in bug_map or bug_id in regressor_bug_ids:
                bug_to_commits[bug_id].append(commit)

        # All bugs blocking the "fuzz" bug and its dependent meta bugs are fuzzing bugs.
        fuzzblocker_bugs = set(
            bug["id"] for bug in bug_map.values() if is_fuzzblocker(bug)
        )
        fuzzing_bugs = (
            set(
                sum(
                    self.get_blocking_of([FUZZING_METABUG_ID], meta_only=True).values(),
                    [],
                )
                + [
                    bug["id"]
                    for bug in bug_map.values()
                    if "bugmon" in bug["whiteboard"].lower()
                    or "bugmon" in bug["keywords"]
                ]
            )
            | fuzzblocker_bugs
        )

        logger.info("Retrieve Phabricator revisions linked to commits...")
        revision_ids = list(
            filter(
                None,
                (
                    repository.get_revision_id(commit)
                    for bug_id in bugs
                    for commit in bug_to_commits[bug_id]
                ),
            )
        )

        logger.info("Retrieve Phabricator revisions linked to bugs...")
        for bug_id in bugs:
            if bug_id not in bug_map:
                continue

            revision_ids += bugzilla.get_revision_ids(bug_map[bug_id])

        logger.info("Download revisions of interest...")
        phabricator.download_modified_revisions()
        phabricator.download_revisions(revision_ids)

        revision_map = {}
        bug_to_revisions_map = collections.defaultdict(list)
        revision_ids_set = set(revision_ids)
        for revision in phabricator.get_revisions():
            if revision["id"] in revision_ids_set:
                revision_map[revision["id"]] = revision

            if (
                revision["fields"]["bugzilla.bug-id"]
                and int(revision["fields"]["bugzilla.bug-id"]) in bug_map
            ):
                bug_to_revisions_map[int(revision["fields"]["bugzilla.bug-id"])].append(
                    revision
                )

        blocker_to_meta = collections.defaultdict(set)
        for meta_bug, blocker_bug_ids in meta_bugs.items():
            for blocker_bug_id in blocker_bug_ids:
                blocker_to_meta[blocker_bug_id].add(meta_bug)

        def find_risk_band(risk: float) -> str:
            for name, start, end in self.risk_bands:
                if start <= risk <= end:
                    return name

            assert False

        def get_commit_data(commit_list: List[repository.CommitDict]) -> List[dict]:
            if len(commit_list) == 0:
                return []

            # Evaluate risk of commits associated to this bug.
            probs = self.regressor_model.classify(commit_list, probabilities=True)

            commits_data = []
            for i, commit in enumerate(commit_list):
                revision_id = repository.get_revision_id(commit)
                if revision_id in revision_map:
                    revision = revision_map[revision_id]

                    testing = phabricator.get_testing_project(revision)
                    if testing is None:
                        testing = "missing"
                else:
                    testing = None

                commits_data.append(
                    {
                        "id": commit["node"],
                        "rev_id": revision_id,
                        "testing": testing,
                        "risk": float(probs[i][1]),
                        "backedout": bool(commit["backedoutby"]),
                        "author": commit["author_email"],
                        "reviewers": commit["reviewers"],
                        "coverage": [
                            commit["cov_added"],
                            commit["cov_covered"],
                            commit["cov_unknown"],
                        ],
                    }
                )

            return commits_data

        component_team_mapping = get_component_team_mapping()

        bug_summaries = []
        for bug_id in bugs:
            if bug_id not in bug_map:
                continue

            commit_list = bug_to_commits.get(bug_id, [])
            commit_data = get_commit_data(commit_list)

            bug = bug_map[bug_id]

            time_to_bug = None
            for regressor_bug_id in bug["regressed_by"]:
                # Get the date of the last commit in the regressor bug that landed before the regression bug.
                last_commit_date = max(
                    (
                        dateutil.parser.parse(commit["pushdate"])
                        for commit in bug_to_commits.get(regressor_bug_id, [])
                        if dateutil.parser.parse(commit["pushdate"])
                        < dateutil.parser.parse(bug["creation_time"]).replace(
                            tzinfo=None
                        )
                    ),
                    default=None,
                )

                if last_commit_date is None:
                    continue

                # Get the minimum "time to bug" (from the fix time of the closest regressor to the regression bug).
                cur_time_to_bug = (
                    dateutil.parser.parse(bug["creation_time"]).replace(tzinfo=None)
                    - last_commit_date
                ).total_seconds() / 86400
                if time_to_bug is None or cur_time_to_bug < time_to_bug:
                    time_to_bug = cur_time_to_bug

            time_to_confirm = None
            if bug["is_confirmed"]:
                for history in bug["history"]:
                    for change in history["changes"]:
                        if (
                            change["field_name"] == "status"
                            and change["removed"] == "UNCONFIRMED"
                            and change["added"] in ("NEW", "ASSIGNED")
                        ):
                            time_to_confirm = (
                                dateutil.parser.parse(history["when"]).replace(
                                    tzinfo=None
                                )
                                - dateutil.parser.parse(bug["creation_time"]).replace(
                                    tzinfo=None
                                )
                            ).total_seconds() / 86400
                            break

                    if time_to_confirm is not None:
                        break

            max_risk = (
                max(commit["risk"] for commit in commit_data)
                if len(commit_data)
                else None
            )

            revisions = []
            for revision in bug_to_revisions_map[bug["id"]]:
                first_review_time = phabricator.get_first_review_time(revision)
                pending_review_time = phabricator.get_pending_review_time(revision)
                revisions.append(
                    {
                        "id": revision["id"],
                        "first_review_time": first_review_time.total_seconds() / 86400
                        if first_review_time is not None
                        else None,
                        "pending_review_time": pending_review_time.total_seconds()
                        / 86400
                        if pending_review_time is not None
                        else None,
                    }
                )

            bug_summary = {
                "id": bug_id,
                "regressor": bug_id in regressor_bug_ids,
                "regression": len(bug["regressed_by"]) > 0
                or any(
                    keyword in bug["keywords"]
                    for keyword in ["regression", "talos-regression"]
                )
                or (
                    "cf_has_regression_range" in bug
                    and bug["cf_has_regression_range"] == "yes"
                ),
                "time_to_bug": time_to_bug,
                "time_to_confirm": time_to_confirm,
                "time_to_assign": bug_features.get_time_to_assign(bug),
                "whiteboard": bug["whiteboard"],
                "assignee": bug["assigned_to"]
                if bug["assigned_to"] != "nobody@mozilla.org"
                else None,
                "versions": bugzilla.get_fixed_versions(bug),
                "component": get_full_component(bug),
                "team": component_team_mapping.get(bug["product"], {}).get(
                    bug["component"]
                ),
                "summary": bug["summary"],
                "fixed": bug["status"] in ("VERIFIED", "RESOLVED"),
                "types": bug_to_types(bug, bug_map),
                "severity": bug["severity"],
                "creation_date": dateutil.parser.parse(bug["creation_time"]).strftime(
                    "%Y-%m-%d"
                ),
                "date": max(
                    dateutil.parser.parse(commit["pushdate"]) for commit in commit_list
                ).strftime("%Y-%m-%d")
                if len(commit_list) > 0
                else None,
                "commits": commit_data,
                "meta_ids": list(blocker_to_meta[bug_id]),
                "risk": max_risk,
                "risk_band": find_risk_band(max_risk) if max_risk is not None else None,
                "fuzz": "b"
                if bug["id"] in fuzzblocker_bugs
                else "y"
                if bug["id"] in fuzzing_bugs
                else "n",
                "revisions": revisions,
            }

            self.get_prev_bugs_stats(bug_summary, commit_list)

            bug_summaries.append(bug_summary)

        landings_by_date = collections.defaultdict(list)
        for bug_summary in bug_summaries:
            landings_by_date[bug_summary["creation_date"]].append(bug_summary)

        with open("landings_by_date.json", "w") as f:
            output: dict = {
                "summaries": landings_by_date,
            }
            if meta_bugs is not None:
                output["featureMetaBugs"] = [
                    {"id": meta_bug, "summary": bug_map[meta_bug]["summary"]}
                    for meta_bug in meta_bugs
                ]

            json.dump(output, f)

    def generate_component_connections(
        self, bug_map: Dict[int, bugzilla.BugDict], bugs: List[int]
    ) -> None:
        bugs_set = set(bugs)
        commits = [
            commit
            for commit in repository.get_commits()
            if commit["bug_id"] in bugs_set
        ]
        commit_map = {commit["node"]: commit for commit in commits}

        # Retrieve components of test failures that occurred when landing patches to fix bugs in specific components.
        component_failures = collections.defaultdict(list)

        push_data_iter, push_data_count, all_runnables = test_scheduling.get_push_data(
            "group"
        )

        for revisions, _, _, possible_regressions, likely_regressions in tqdm(
            push_data_iter(), total=push_data_count
        ):
            commit_list = [
                commit_map[revision] for revision in revisions if revision in commit_map
            ]
            if len(commit_list) == 0:
                continue

            commit_bugs = [
                bug_map[commit["bug_id"]]
                for commit in commit_list
                if commit["bug_id"] in bug_map
            ]

            components = list(set(get_full_component(bug) for bug in commit_bugs))

            groups = [
                group
                for group in list(set(possible_regressions + likely_regressions))
                if group.encode("utf-8") in self.path_to_component
            ]

            for group in groups:
                for component in components:
                    component_failures[component].append(
                        self.path_to_component[group.encode("utf-8")]
                        .tobytes()
                        .decode("utf-8")
                    )

        # Filter out commits for which we have no bugs.
        commits = [commit for commit in commits if commit["bug_id"] in bug_map]

        # Sort commits by bug component, so we can use itertools.groupby to group them by bug component.
        commits.sort(key=lambda x: get_full_component(bug_map[x["bug_id"]]))

        commit_groups = []
        for component, commit_iter in itertools.groupby(
            commits, lambda x: get_full_component(bug_map[x["bug_id"]])
        ):
            commit_group = {
                "component": component,
                "most_common_test_failure_components": histogram(
                    component_failures[component]
                )
                if component in component_failures
                else {},
            }
            self.get_prev_bugs_stats(
                commit_group,
                list(commit_iter),
                component,
            )
            commit_groups.append(commit_group)

        with open("component_connections.json", "w") as f:
            json.dump(commit_groups, f)

        repository.close_component_mapping()

    def generate_component_test_stats(
        self, bug_map: Dict[int, bugzilla.BugDict], test_infos: Dict[str, Any]
    ) -> None:
        component_test_stats: Dict[
            str, Dict[str, Dict[str, List[Dict[str, int]]]]
        ] = collections.defaultdict(
            lambda: collections.defaultdict(lambda: collections.defaultdict(list))
        )
        for date, test_info in test_infos.items():
            for component, count in test_info["skips"].items():
                component_test_stats[component][date]["skips"] = count

            for bug in test_info["bugs"]:
                bug_id = bug["id"]
                if bug_id not in bug_map:
                    continue

                component_test_stats[get_full_component(bug_map[bug_id])][date][
                    "bugs"
                ].append(bug)

        with open("component_test_stats.json", "w") as f:
            json.dump(component_test_stats, f)

    def go(self, days: int) -> None:
        bugs = self.get_landed_and_filed_since(days)

        meta_bugs = self.get_meta_bugs(days)

        last_modified = db.last_modified(bugzilla.BUGS_DB)
        logger.info(f"Deleting bugs modified since the last run on {last_modified}")
        changed_ids = bugzilla.get_ids(
            {"f1": "delta_ts", "o1": "greaterthaneq", "v1": last_modified.date()}
        )
        bugzilla.delete_bugs(lambda bug: bug["id"] in changed_ids)

        bugs = list(set(bugs))

        test_infos = self.retrieve_test_info(days)
        test_info_bugs: List[int] = [
            bug["id"] for test_info in test_infos.values() for bug in test_info["bugs"]
        ]

        logger.info("Download bugs of interest...")
        bugzilla.download_bugs(bugs + test_info_bugs + [FUZZING_METABUG_ID] + meta_bugs)

        logger.info(f"{len(bugs)} bugs to analyze.")

        bugs_set = set(bugs + test_info_bugs + meta_bugs)

        bug_map = {}
        regressor_bug_ids = set()
        for bug in bugzilla.get_bugs():
            # Only add to the map bugs we are interested in, and bugs that block other bugs (needed for the bug_to_types call).
            if bug["id"] in bugs_set or len(bug["blocks"]) > 0:
                bug_map[bug["id"]] = bug

            if len(bug["regressions"]) > 0:
                regressor_bug_ids.add(bug["id"])

        self.generate_landings_by_date(
            bug_map, regressor_bug_ids, bugs, self.get_blocking_of(meta_bugs)
        )

        self.generate_component_connections(bug_map, bugs)

        self.generate_component_test_stats(bug_map, test_infos)


def notification(days: int) -> None:
    with open("landings_by_date.json", "r") as f:
        data = json.load(f)
        bug_summaries: list[dict] = sum(data["summaries"].values(), [])
        feature_meta_bugs = {
            meta_bug["id"]: meta_bug["summary"] for meta_bug in data["featureMetaBugs"]
        }

    bug_summary_ids = {bug_summary["id"] for bug_summary in bug_summaries}

    bug_map = {}
    for b in bugzilla.get_bugs():
        if b["id"] in bug_summary_ids:
            bug_map[b["id"]] = b

    with open("component_test_stats.json", "r") as f:
        component_test_stats = json.load(f)

    all_teams = set(bug["team"] for bug in bug_summaries if bug["team"] is not None)
    all_teams.remove("Other")
    all_teams.remove("Mozilla")

    tracking_flag_re = re.compile("cf_tracking_firefox([0-9]+)")

    def get_tracking_info(bug):
        versions = []
        for flag, value in bug.items():
            if value not in ("+", "blocking"):
                continue

            match = tracking_flag_re.search(flag)
            if match is None:
                continue

            versions.append(match.group(1))

        return versions

    r = requests.get("https://product-details.mozilla.org/1.0/firefox_versions.json")
    nightly_ver = int(r.json()["FIREFOX_NIGHTLY"].split(".")[0])
    beta_ver = nightly_ver - 1
    release_ver = beta_ver - 1

    team_data: dict[str, Any] = {}
    for team in all_teams:
        cur_team_data = team_data[team] = {}
        cur_team_data["month_changes"] = 0
        cur_team_data["high_risk_changes"] = 0
        cur_team_data["medium_risk_changes"] = 0
        cur_team_data["low_risk_changes"] = 0
        cur_team_data["prev_high_risk_changes"] = 0
        cur_team_data["new_regressions"] = 0
        cur_team_data["fixed_new_regressions"] = 0
        cur_team_data["unassigned_new_regressions"] = 0
        cur_team_data["new_crash_regressions"] = 0
        cur_team_data["week_old_fixed_regressions"] = 0
        cur_team_data["month_old_fixed_regressions"] = 0
        cur_team_data["more_than_month_old_fixed_regressions"] = 0
        cur_team_data["unfixed_regressions"] = []
        cur_team_data["fix_times"] = []
        cur_team_data["prev_fix_times"] = []
        cur_team_data["patch_coverage_covered"] = 0
        cur_team_data["patch_coverage_added"] = 0
        cur_team_data["coverage_patches"] = []
        cur_team_data["open_review_times"] = []
        cur_team_data["review_times"] = []
        cur_team_data["prev_review_times"] = []
        cur_team_data["intermittent_failures"] = collections.defaultdict(int)

        cur_team_data["carryover_regressions"] = 0
        cur_team_data["affecting_carryover_regressions"] = []

    carrytest = set()
    for bug in bug_summaries:
        if bug["team"] in ("Other", "Mozilla"):
            continue

        cur_team_data = team_data[bug["team"]]

        creation_date = dateutil.parser.parse(bug["creation_date"])
        fix_date = (
            dateutil.parser.parse(bug["date"]) if bug["date"] is not None else None
        )

        if not bug["fixed"]:
            for revision in bug["revisions"]:
                if revision["pending_review_time"] is None:
                    continue

                cur_team_data["open_review_times"].append(
                    (revision["pending_review_time"], revision["id"])
                )

        if fix_date is not None and fix_date > datetime.utcnow() - relativedelta(
            months=1
        ):
            cur_team_data["month_changes"] += 1

            if fix_date > datetime.utcnow() - relativedelta(weeks=1):
                cur_team_data["fix_times"].append(
                    (fix_date - creation_date).total_seconds() / 86400
                )

                if bug["risk_band"] == "h":
                    cur_team_data["high_risk_changes"] += 1
                elif bug["risk_band"] == "m":
                    cur_team_data["medium_risk_changes"] += 1
                elif bug["risk_band"] == "l":
                    cur_team_data["low_risk_changes"] += 1

                if bug["regression"]:
                    if creation_date > datetime.utcnow() - relativedelta(weeks=1):
                        cur_team_data["week_old_fixed_regressions"] += 1
                    elif creation_date > datetime.utcnow() - relativedelta(months=1):
                        cur_team_data["month_old_fixed_regressions"] += 1
                    else:
                        cur_team_data["more_than_month_old_fixed_regressions"] += 1

                cur_team_data["review_times"] += [
                    revision["first_review_time"]
                    for revision in bug["revisions"]
                    if revision["first_review_time"] is not None
                ]

                for commit in bug["commits"]:
                    lines_added = 0
                    lines_covered = 0
                    if commit["coverage"] and commit["coverage"][0] is not None:
                        lines_added += commit["coverage"][0]
                        # Consider unknown as covered.
                        lines_covered += commit["coverage"][1] + commit["coverage"][2]

                    cur_team_data["patch_coverage_added"] += lines_added
                    cur_team_data["patch_coverage_covered"] += lines_covered

                    if lines_added != 0:
                        cur_team_data["coverage_patches"].append(
                            (lines_covered / lines_added, commit["id"])
                        )

            elif fix_date > datetime.utcnow() - relativedelta(weeks=2):
                cur_team_data["prev_fix_times"].append(
                    (fix_date - creation_date).total_seconds() / 86400
                )

                if bug["risk_band"] == "h":
                    cur_team_data["prev_high_risk_changes"] += 1

                cur_team_data["prev_review_times"] += [
                    revision["first_review_time"]
                    for revision in bug["revisions"]
                    if revision["first_review_time"] is not None
                ]

        if bug["regression"] and creation_date > datetime.utcnow() - relativedelta(
            weeks=2
        ):
            cur_team_data["new_regressions"] += 1
            if bug["team"] == "Compiler and Development Tools":
                print("New regression: {}".format(bug["id"]))
            if "crash" in bug["types"]:
                cur_team_data["new_crash_regressions"] += 1

            if bug["fixed"]:
                cur_team_data["fixed_new_regressions"] += 1
            elif bug["assignee"] is None:
                cur_team_data["unassigned_new_regressions"] += 1

        if creation_date > datetime.utcnow() - relativedelta(weeks=2):
            if bug["regression"] and not bug["fixed"]:
                if bug["team"] == "Compiler and Development Tools":
                    print("Unfixed regression: {}".format(bug["id"]))
                cur_team_data["unfixed_regressions"].append(bug)

        if creation_date > datetime.utcnow() - relativedelta(days=days):
            if bug["regression"] and not bug["fixed"]:
                cur_team_data["carryover_regressions"] += 1

                if bug["team"] == "DOM":
                    carrytest.add(bug["id"])

                full_bug = bug_map[bug["id"]]
                if (
                    "stalled" not in full_bug["keywords"]
                    and "intermittent-failure" not in full_bug["keywords"]
                ):
                    for version in [nightly_ver, beta_ver, release_ver]:
                        if (
                            f"cf_status_firefox{version}" in full_bug
                            and full_bug[f"cf_status_firefox{version}"] == "affected"
                            and full_bug[f"cf_tracking_firefox{version}"] != "-"
                            and f"cf_status_firefox{version - 1}" in full_bug
                            and full_bug[f"cf_status_firefox{version - 1}"]
                            not in ("unaffected", "?", "---")
                        ):
                            cur_team_data["affecting_carryover_regressions"].append(bug)
                            break

    component_team_mapping = get_component_team_mapping()
    for product_component, day_to_data in component_test_stats.items():
        product, component = product_component.split("::")
        team = component_team_mapping.get(product, {}).get(component)
        if team is None or team in ("Other", "Mozilla"):
            continue
        cur_team_data = team_data[team]

        for day, data in day_to_data.items():
            if "bugs" not in data:
                continue

            if dateutil.parser.parse(day) < datetime.utcnow() - relativedelta(weeks=1):
                continue

            for bug in data["bugs"]:
                cur_team_data["intermittent_failures"][bug["id"]] += bug["count"]

    total_carryover_regressions = sum(
        cur_team_data["carryover_regressions"] for cur_team_data in team_data.values()
    )

    print("New regressions")
    print(
        {
            cur_team: cur_team_data["new_regressions"]
            for cur_team, cur_team_data in team_data.items()
        }
    )
    print("Month changes:")
    print(
        {
            cur_team: cur_team_data["month_changes"]
            for cur_team, cur_team_data in team_data.items()
        }
    )
    print("Rates:")
    print(
        {
            cur_team: cur_team_data["new_regressions"] / cur_team_data["month_changes"]
            if cur_team_data["month_changes"] != 0
            else 0.0
            for cur_team, cur_team_data in team_data.items()
        }
    )

    average_patch_coverage = round(
        100
        * sum(
            cur_team_data["patch_coverage_covered"]
            for cur_team_data in team_data.values()
        )
        / sum(
            cur_team_data["patch_coverage_added"]
            for cur_team_data in team_data.values()
        ),
        1,
    )
    average_median_fix_time = statistics.mean(
        statistics.median(cur_team_data["fix_times"])
        for cur_team_data in team_data.values()
        if len(cur_team_data["fix_times"]) > 0
    )

    all_intermittent_failures: dict[int, int] = collections.defaultdict(int)
    for cur_team_data in team_data.values():
        for bug_id, count in cur_team_data["intermittent_failures"].items():
            all_intermittent_failures[bug_id] += count

    sorted_intermittent_failures = sorted(
        all_intermittent_failures.items(), key=lambda x: -x[1]
    )
    intermittent_failure_positions = {
        bug_id: i + 1 for i, (bug_id, count) in enumerate(sorted_intermittent_failures)
    }

    def regression_to_text(bug):
        full_bug = bug_map[bug["id"]]

        unfixed_regressions_components = []

        tracked = get_tracking_info(full_bug)
        if len(tracked) > 0:
            unfixed_regressions_components.append(
                "Tracked for versions {}".format(", ".join(tracked))
            )

        if len(bug["meta_ids"]) > 0:
            features = ", ".join(
                f"'{feature_meta_bugs[meta_id]}'" for meta_id in bug["meta_ids"]
            )
            unfixed_regressions_components.append(f"Blocked features: {features}")

        if full_bug["status"] == "ASSIGNED":
            unfixed_regressions_components.append("Assigned")
        else:
            unfixed_regressions_components.append("Unassigned")

        hours = math.ceil(
            (
                datetime.utcnow()
                - dateutil.parser.parse(full_bug["last_change_time"]).replace(
                    tzinfo=None
                )
            ).total_seconds()
            / 3600
        )
        if hours > 24:
            unfixed_regressions_components.append(
                f"Last activity {math.ceil(hours / 24)} days ago"
            )
        else:
            unfixed_regressions_components.append(f"Last activity {hours} hours ago")

        return "- https://bugzilla.mozilla.org/show_bug.cgi?id={} - {}".format(
            bug["id"], ", ".join(unfixed_regressions_components)
        )

    notify = taskcluster.Notify(get_taskcluster_options())

    team_to_receivers = json.loads(get_secret("NOTIFICATION_TEAMS"))
    for team, cur_team_data in team_data.items():
        if team not in team_to_receivers:
            continue

        month_changes = cur_team_data["month_changes"]
        high_risk_changes = cur_team_data["high_risk_changes"]
        predicted_regressions = round(
            0.33 * cur_team_data["high_risk_changes"]
            + 0.16 * cur_team_data["medium_risk_changes"]
            + 0.06 * cur_team_data["low_risk_changes"],
            1,
        )
        prev_high_risk_changes = cur_team_data["prev_high_risk_changes"]
        new_regressions = cur_team_data["new_regressions"]
        fixed_new_regressions = cur_team_data["fixed_new_regressions"]
        unassigned_new_regressions = cur_team_data["unassigned_new_regressions"]
        new_crash_regressions = cur_team_data["new_crash_regressions"]
        carryover_regressions = cur_team_data["carryover_regressions"]
        prev_carryover_regressions = (
            cur_team_data["carryover_regressions"]
            - cur_team_data["new_regressions"]
            + cur_team_data["week_old_fixed_regressions"]
            + cur_team_data["month_old_fixed_regressions"]
            + cur_team_data["more_than_month_old_fixed_regressions"]
        )

        # Calculate median regression rate for similarly sized teams (teams that land at least
        # a fifth of the changes of the current team and less than five times the changes of the
        # current team).
        median_regression_rate = statistics.median(
            ctd["new_regressions"] / ctd["month_changes"]
            for ctd in team_data.values()
            if ctd["month_changes"] > cur_team_data["month_changes"] / 5
            and ctd["month_changes"] < cur_team_data["month_changes"] * 5
        )

        unfixed_regressions = "\n".join(
            regression_to_text(reg)
            for reg in sorted(
                cur_team_data["unfixed_regressions"],
                key=lambda bug: bug_map[bug["id"]]["last_change_time"],
            )
        )
        affecting_carryover_regressions = "\n".join(
            regression_to_text(reg)
            for reg in sorted(
                cur_team_data["affecting_carryover_regressions"],
                key=lambda bug: bug_map[bug["id"]]["last_change_time"],
            )
        )

        if len(cur_team_data["fix_times"]) > 1:
            fix_time_deciles = statistics.quantiles(
                cur_team_data["fix_times"], n=10, method="inclusive"
            )
            median_fix_time = round(fix_time_deciles[4], 1)
            ninth_decile_fix_time = round(fix_time_deciles[8], 1)
        elif len(cur_team_data["fix_times"]) > 0:
            median_fix_time = round(cur_team_data["fix_times"][0], 1)
            ninth_decile_fix_time = round(cur_team_data["fix_times"][0], 1)
        else:
            median_fix_time = None
            ninth_decile_fix_time = None

        if len(cur_team_data["prev_fix_times"]) > 0:
            prev_median_fix_time = round(
                statistics.median(cur_team_data["prev_fix_times"]), 1
            )
        else:
            prev_median_fix_time = None

        if median_fix_time is not None and prev_median_fix_time is not None:
            verb = (
                "improving"
                if median_fix_time < prev_median_fix_time
                else "worsening"
                if prev_median_fix_time < median_fix_time
                else "staying constant"
            )
            fix_time_diff = f"This is {verb} when compared to two weeks ago (median was {prev_median_fix_time} days)."
        else:
            fix_time_diff = ""

        top_intermittent_failures = "\n".join(
            f"- https://bugzilla.mozilla.org/show_bug.cgi?id={bug_id} - {count} failures (this is {intermittent_failure_positions[bug_id]} overall)"
            for bug_id, count in sorted(
                cur_team_data["intermittent_failures"].items(), key=lambda x: -x[1]
            )[:7]
        )

        patch_coverage = round(
            100
            * cur_team_data["patch_coverage_covered"]
            / cur_team_data["patch_coverage_added"]
            if cur_team_data["patch_coverage_added"] != 0
            else 100,
            1,
        )
        low_coverage_patches = "\n".join(
            f"- https://phabricator.services.mozilla.com/D{rev_id} - {pc}%"
            for pc, rev_id in sorted(
                cur_team_data["coverage_patches"], key=lambda x: x[0]
            )[:7]
        )

        if len(cur_team_data["review_times"]) > 1:
            review_time_deciles = statistics.quantiles(
                cur_team_data["review_times"],
                n=10,
                method="inclusive",
            )
            median_first_review_time = round(review_time_deciles[4], 1)
            ninth_decile_first_review_time = round(review_time_deciles[8], 1)
        elif len(cur_team_data["review_times"]) > 0:
            median_first_review_time = round(cur_team_data["review_times"], 1)
            ninth_decile_first_review_time = round(cur_team_data["review_times"], 1)
        else:
            median_first_review_time = None
            ninth_decile_first_review_time = None

        if len(cur_team_data["prev_review_times"]) > 0:
            prev_median_first_review_time = round(
                statistics.median(cur_team_data["prev_review_times"]), 1
            )
        else:
            prev_median_first_review_time = None

        if (
            median_first_review_time is not None
            and prev_median_first_review_time is not None
        ):
            verb = (
                "improving"
                if median_first_review_time < prev_median_first_review_time
                else "worsening"
                if prev_median_first_review_time < median_first_review_time
                else "staying constant"
            )
            review_time_diff = f"This is {verb} when compared to two weeks ago (median was {prev_median_first_review_time} days)."
        else:
            review_time_diff = ""

        slow_review_patches = "\n".join(
            f"- https://phabricator.services.mozilla.com/D{rev_id} - {round(review_time, 1)} days"
            for review_time, rev_id in sorted(
                cur_team_data["open_review_times"], key=lambda x: -x[0]
            )[:7]
        )

        report_url_querystring = urllib.parse.urlencode({"teams": team})

        notification = f"""<b>NEW REGRESSIONS</b>

{new_regressions} new regressions ({new_crash_regressions} crashes) during the past two weeks. {fixed_new_regressions} of them were fixed, {unassigned_new_regressions} are still unassigned.

The regression rate (regressions from the past two weeks / changes from this month) is {round(new_regressions / month_changes, 2)} ({"higher than" if new_regressions / month_changes > median_regression_rate else "lower than" if median_regression_rate > new_regressions / month_changes else "equal to"} the median of {round(median_regression_rate, 2)} across other teams).
This week your team committed {high_risk_changes} high risk changes, {"more than" if high_risk_changes > prev_high_risk_changes else "less than" if prev_high_risk_changes > high_risk_changes else "equal to"} {prev_high_risk_changes} from last week.
Based on historical information, your past week changes are likely to cause {predicted_regressions} regressions in the future.

Unfixed regressions from the past two weeks:
{unfixed_regressions}

The median time to fix for regressions fixed in the past week was {median_fix_time} days ({"higher than" if median_fix_time > average_median_fix_time else "lower than" if average_median_fix_time > median_fix_time else "equal to"} the average of {round(average_median_fix_time, 1)} across other teams). {fix_time_diff}
90% of bugs were fixed within {ninth_decile_fix_time} days.
<br />
<b>CARRYOVER REGRESSIONS</b>

There are {carryover_regressions} carryover regressions in your team out of a total of {total_carryover_regressions} in Firefox, {"increasing" if carryover_regressions > prev_carryover_regressions else "reducing" if prev_carryover_regressions > carryover_regressions else "staying constant"} from {prev_carryover_regressions} you had last week."""

        if len(affecting_carryover_regressions) > 0:
            notification += f"""<br /><br />Carryover regressions which are still tracked as affecting Release, Beta or Nightly:
{affecting_carryover_regressions}"""

        notification += f"""

<br />
<b>INTERMITTENT FAILURES</b>
<br />
<br />

Top intermittent failures from the past week:
{top_intermittent_failures}

<br />
<b>TEST COVERAGE</b>
<br />
<br />

Total coverage for patches landing this past week was {patch_coverage}% ({"higher" if patch_coverage > average_patch_coverage else "lower"} than the average across other teams, {average_patch_coverage}%).
"""

        if len(low_coverage_patches) > 0:
            notification += f"""<br />List of lowest coverage patches:
{low_coverage_patches}"""

        notification += f"""

<br />
<b>REVIEW</b>
<br />
<br />

The median time to first review patches for last week's fixed bugs was {median_first_review_time} days. 90% of patches were first reviewed within {ninth_decile_first_review_time} days. {review_time_diff}

List of revisions that are still waiting for a review:
{slow_review_patches}


Find the full report with fancy charts at https://changes.moz.tools/team.html?{report_url_querystring}.

Report bugs or enhancement requests on https://github.com/mozilla/bugbug or to mcastelluccio@mozilla.com.
"""

        receivers = team_to_receivers[team]
        for receiver in receivers:
            notify.email(
                {
                    "address": receiver,
                    "subject": f"Quality report for '{team}'",
                    "content": notification,
                    "template": "fullscreen",
                }
            )


def main() -> None:
    description = "Generate risk report of recent landings"
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("repo_dir", help="Path to a Gecko repository.")
    parser.add_argument(
        "--days",
        type=int,
        help="How many days of commits to analyze.",
        required=True,
    )
    args = parser.parse_args()

    landings_risk_report_generator = LandingsRiskReportGenerator(args.repo_dir)
    landings_risk_report_generator.go(args.days)
    if datetime.today().isoweekday() == get_secret("NOTIFICATION_DAY"):
        notification(args.days)


if __name__ == "__main__":
    main()
