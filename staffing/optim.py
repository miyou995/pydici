# coding: utf-8
"""
Optimisation tools for pydici staffing module
@author: Sébastien Renard (sebastien.renard@digitalfox.org)
@license: AGPL v3 or newer (http://www.gnu.org/licenses/agpl-3.0.html)
"""

from datetime import datetime

from ortools.sat.python import cp_model

from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.utils.translation import  gettext_noop
from django.utils.html import escape
from django.db import transaction

from core.utils import working_days, to_int_or_round
from staffing.models import Holiday, Staffing


def solve_pdc(consultants, senior_consultants, director_consultants, missions, months, missions_charge, missions_remaining, missions_boundaries,
              consultants_freetime, predefined_assignment, exclusions, consultants_rates, solver_param=None):
    # default value
    if solver_param is None:
        solver_param = {}
    director_quota = solver_param.get("director_quota", 10)
    senior_quota = solver_param.get("senior_quota", 20)
    newbie_quota = solver_param.get("newbie_quota", 40)
    planning_weight = solver_param.get("planning_weight", 1)
    mission_per_people_weight = solver_param.get("mission_per_people_weight", 1)
    people_per_mission_weight = solver_param.get("people_per_mission_weight", 1)
    freetime_weight = solver_param.get("freetime_weight", 1)
    newbie_consultants = [c for c in consultants if c not in senior_consultants and c not in director_consultants]
    # CP-SAT model
    model = cp_model.CpModel()
    # variable we are searching
    staffing = {}  # Per month
    staffing_cum = {}  # cumulative staffing since mission beginning
    staffing_b = {}  # Bool indicating if consultant is staffed  (ie staffing >0) by month on this mission
    staffing_b_all = {}  # Bool indicating if consultant is staffed on this mission
    staffing_mission_delta = {}  # Delta between proposition and forecast for mission/month
    staffing_mission_cum_delta = {}  # Cumulated Delta between proposition and forecast for mission/month
    staffing_mission_global_delta = {}  # Delta between proposition and forecast for mission accross all month
    for consultant in consultants:
        if consultant not in staffing:
            staffing[consultant] = {}
            staffing_cum[consultant] = {}
            staffing_b[consultant] = {}
            staffing_b_all[consultant] = {}
        for mission in missions:
            if mission not in staffing[consultant]:
                staffing[consultant][mission] = {}
                staffing_cum[consultant][mission] = {}
                staffing_b[consultant][mission] = {}
            for month_num, month in enumerate(months):
                # Define vars
                staffing[consultant][mission][month] = model.NewIntVar(0, consultants_freetime[consultant][month],
                                                                       "staffing[%s,%s,%s]" % (consultant, mission, month))
                staffing_cum[consultant][mission][month] = model.NewIntVar(0, sum(consultants_freetime[consultant].values()),
                                                                           "staffing_cum[%s,%s,%s]" % (consultant, mission, month))
                staffing_b[consultant][mission][month] = model.NewBoolVar(
                    "staffing_b[%s,%s,%s]" % (consultant, mission, month))
                # Links vars staffing and staffing_b
                model.Add(staffing[consultant][mission][month] > 0).OnlyEnforceIf(staffing_b[consultant][mission][month])
                model.Add(staffing[consultant][mission][month] == 0).OnlyEnforceIf(
                    staffing_b[consultant][mission][month].Not())
                # Links vars staffing and staffing_cum
                model.Add(sum(staffing[consultant][mission][month] for month in months[:months.index(month) + 1]) ==
                          staffing_cum[consultant][mission][month])

            # Define vars staffing_b_all
            staffing_b_all[consultant][mission] = model.NewBoolVar("staffing_b_all[%s,%s]" % (consultant, mission))
            # Links vars staffing and staffing_b_all
            model.Add(sum(staffing[consultant][mission][month] for month in months) > 0).OnlyEnforceIf(
                staffing_b_all[consultant][mission])
            model.Add(sum(staffing[consultant][mission][month] for month in months) == 0).OnlyEnforceIf(
                staffing_b_all[consultant][mission].Not())

    # Define monthly delta between proposition and forecast
    for mission in missions:
        if mission not in staffing_mission_delta:
            staffing_mission_delta[mission] = {}
            staffing_mission_cum_delta[mission] = {}
        for month in months:
            staffing_mission_delta[mission][month] = model.NewIntVar(0, 1000,
                                                                     "staffing_mission_delta[%s,%s]" % (mission, month))
            staffing_mission_cum_delta[mission][month] = model.NewIntVar(0, 1000,
                                                                         "staffing_mission_cum_delta[%s,%s]" % (mission, month))
            month_total = sum(staffing[consultant][mission][month] for consultant in consultants)
            model.Add(month_total >= missions_charge[mission][month] - staffing_mission_delta[mission][month])
            model.Add(month_total <= missions_charge[mission][month] + staffing_mission_delta[mission][month])
            cum_staffing = sum(staffing_cum[consultant][mission][month] for consultant in consultants)
            cum_charge = sum(missions_charge[mission][month] for month in months[:months.index(month) + 1])
            model.Add(cum_staffing >= cum_charge - staffing_mission_cum_delta[mission][month])
            model.Add(cum_staffing <= cum_charge + staffing_mission_cum_delta[mission][month])

    # Define global delta between proposition and forecast
    for mission in missions:
        staffing_mission_global_delta[mission] = model.NewIntVar(-1000, 1000, "staffing_mission_global_delta[%s]" % mission)
        s = []
        mission_total = sum(missions_charge[mission][month] for month in months)
        for month in months:
            s.extend(staffing[consultant][mission][month] for consultant in consultants)
        model.Add(sum(s) >= mission_total - staffing_mission_global_delta[mission])
        model.Add(sum(s) <= mission_total + staffing_mission_global_delta[mission])

    # Each mission should have a noob, senior and director quota each month
    for mission in missions:
        for month in months:
            newbie_charge = sum(staffing[consultant][mission][month] for consultant in newbie_consultants)
            senior_charge = sum(staffing[consultant][mission][month] for consultant in senior_consultants)
            director_charge = sum(staffing[consultant][mission][month] for consultant in director_consultants)
            total_charge = newbie_charge + senior_charge + director_charge
            model.Add(total_charge * newbie_quota <= newbie_charge * 100)
            model.Add(total_charge * senior_quota <= senior_charge * 100)
            model.Add(total_charge * director_quota <= director_charge * 100)

    # All missions are done, but not overshoot
    for mission in missions:
        s_amount = []
        for month in months:
            s_amount.extend(staffing[consultant][mission][month] * consultants_rates[consultant][mission] for consultant in consultants)
        model.Add(sum(s_amount) <= missions_remaining[mission])  # Don't overshoot mission price

    # Consultant have limited free time
    for consultant in consultants:
        for month in months:
            model.Add(sum(staffing[consultant][mission][month] for mission in missions) <= consultants_freetime[consultant][
                month])

    # Respect predefined assignment
    for mission, assigned_consultants in predefined_assignment.items():
        for consultant in assigned_consultants:
            model.Add(sum(staffing[consultant][mission][month] for month in months) > 0)

    # Respect exclusions
    for mission, excluded_consultants in exclusions.items():
        for consultant in excluded_consultants:
            model.AddBoolAnd([staffing_b_all[consultant][mission].Not()])

    # Respect mission boundaries
    for mission in missions:
        for i, month in enumerate(months):
            if (missions_boundaries[mission]["start"] in months and i < months.index(missions_boundaries[mission]["start"])) or \
                    (missions_boundaries[mission]["end"] in months and i >= months.index(missions_boundaries[mission]["end"])):
                model.Add(staffing[consultant][mission][month] == 0)

    # define score components
    planning_score_items = []
    freetime_score_items = []
    mission_per_people_score_items = []
    people_per_mission_score_items = []

    # respect mission planning
    if planning_weight > 0:
        for mission in missions:
            for month in months:
                # add score if cumulated planning delta is not respected
                planning_score_items.append(staffing_mission_cum_delta[mission][month])
                if missions_charge[mission][month] > 0:
                    # add score if month planning is not respected
                    planning_score_items.append(staffing_mission_delta[mission][month])
                else:
                    # add penalty when charge is used outside forecast (late or too early work)
                    planning_score_items.append(3 * sum(staffing[consultant][mission][month] for consultant in consultants))
            # Add score for global planning delta
            planning_score_items.append(4 * staffing_mission_global_delta[mission])

    for consultant in consultants:
        for month in months:
            # optimise freetime and mission per people only if we have stuff to do
            if sum(missions_charge[mission][month] for mission in missions) > 0:
                # reduce free time for newbies only
                if consultant in newbie_consultants and freetime_weight > 0:
                    charge = sum(staffing[consultant][mission][month] for mission in missions)
                    freetime_score_items.append(consultants_freetime[consultant][month] - charge)
                # limit number of mission per people
                mission_per_people_score_items.append(sum(staffing_b[consultant][mission][month] for mission in missions))

    # limit number of people per mission
    for mission in missions:
        people_per_mission_score_items.append(5*sum(staffing_b_all[consultant][mission] for consultant in consultants))

    # Optim part
    # Define intermediate score
    planning_score = model.NewIntVar(0, 1000, "planning_score")
    freetime_score = model.NewIntVar(0, 1000, "freetime_score")
    people_per_mission_score = model.NewIntVar(0, 1000, "people_per_mission_score")
    mission_per_people_score = model.NewIntVar(0, 1000, "mission_per_people_score")
    model.Add(planning_score == planning_weight * sum(planning_score_items))
    model.Add(freetime_score == freetime_weight * sum(freetime_score_items))
    model.Add(people_per_mission_score == people_per_mission_weight * sum(people_per_mission_score_items))
    model.Add(mission_per_people_score == mission_per_people_weight * sum(mission_per_people_score_items))

    score = planning_score + freetime_score + mission_per_people_score + people_per_mission_score
    # optimise model to have minimum score
    model.Minimize(score)

    # Solve it
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0  # Limit to 10 secs
    status = solver.Solve(model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    # Declare status and score string to make them translatable
    gettext_noop("FEASIBLE")
    gettext_noop("OPTIMAL")
    gettext_noop("planning_score")
    gettext_noop("freetime_score")
    gettext_noop("people_per_mission_score")
    gettext_noop("mission_per_people_score")
    return solver, status, [planning_score, freetime_score, people_per_mission_score, mission_per_people_score], staffing


def display_solver_solution(solver, scores, staffing, consultants, missions, months, missions_charge, consultants_freetime):
    """Display on stdout solver solution. For test and dev process only"""
    for score in scores:
        print("%s = %s" % (score.Name(), solver.Value(score)))
    print("Total score = %s" % (sum(solver.Value(score) for score in scores)))
    print()
    print("Mis.\tCons.\t" + "\t".join(months))
    for mission in missions:
        print("-" * 35)
        for consultant in consultants:
            charges = "\t ".join(str(solver.Value(staffing[consultant][mission][month])) for month in months)
            print("%s \t %s \t %s" % (mission, consultant, charges))
        print("%s\t All\t" % mission, end="")
        for month in months:
            mission_charge = sum(solver.Value(staffing[consultant][mission][month]) for consultant in consultants)
            print("%s/%s\t" % (mission_charge, missions_charge[mission][month]), end="")
        print()
    print("-" * 35)
    for consultant in consultants:
        print("All\t %s\t" % consultant, end="")
        for month in months:
            consultant_charge = sum(solver.Value(staffing[consultant][mission][month]) for mission in missions)
            print("%s/%s\t" % (consultant_charge, consultants_freetime[consultant][month]), end="")
        print()


def solver_solution_format(solver, staffing, consultants, missions, staffing_dates, missions_charge, consultants_freetime, consultant_rates):
    """Prepare solver solution for template rendering. Returns staffing_array and mission_remaining_array"""
    results = []
    missions_remaining_results = []
    class_optim_ok = "optim_ok"
    class_optim_info = "optim_info"
    class_optim_warn = "optim_warn"
    for mission in missions:
        mission_id = mission.mission_id()
        mission_link = mark_safe("<a href='%s#tab-timesheet'>%s</a>" % (mission.get_absolute_url(), escape(mission)))
        new_forecast = sum([solver.Value(staffing[consultant.trigramme][mission_id][month[1]]) * consultant_rates[consultant.trigramme][mission_id] / 1000
                            for consultant in consultants for month in staffing_dates])
        new_target_remaining = mission.remaining(mode="current") - new_forecast
        missions_remaining_results.append([mission_link,
                                           to_int_or_round(mission.price or 3),
                                           to_int_or_round(mission.remaining(mode="current"), 3),
                                           to_int_or_round(mission.remaining(mode="target"), 3),
                                           to_int_or_round(new_target_remaining, 3),
                                           ])
        for consultant in consultants:
            consultant_link = mark_safe("<a href='%s#tab-staffing'>%s</a>" % (consultant.get_absolute_url(), escape(consultant)))
            charges = []
            display_consultant = False
            for month in staffing_dates:
                charge = solver.Value(staffing[consultant.trigramme][mission_id][month[1]])
                try:
                    delta = charge - Staffing.objects.get(mission=mission, consultant=consultant, staffing_date=month[0]).charge
                except Staffing.DoesNotExist:
                    delta = charge
                if charge or delta:
                    display_consultant = True
                    class_optim = None
                    if (abs(delta) == charge) or (charge == 0 and delta != 0):  # Notify for newcomers and leavers
                        class_optim = class_optim_info
                    if delta > 0:
                        charges.append((class_optim, mark_safe("%i <small>(+%i)</small>" % (charge, delta))))
                    elif delta < 0:
                        charges.append((class_optim, mark_safe("%i <small>(%i)</small>" % (charge, delta))))
                    else:
                        charges.append((class_optim, "%i" % charge))
                else:
                    charges.append("")  # Don't display zero to ease readability
            # Add charges for mission / consultant if needed
            if display_consultant:
                results.append([mission_link, consultant_link, *charges])
        all_charges = []
        results.append([""] * (len(staffing_dates) + 2))
        for month in staffing_dates:
            mission_charge = sum(solver.Value(staffing[consultant.trigramme][mission_id][month[1]]) for consultant in consultants)
            if mission_charge > 0 or missions_charge[mission_id][month[1]] > 0:
                if abs(missions_charge[mission_id][month[1]] - mission_charge) < 2:
                    class_optim = class_optim_ok
                else:
                    class_optim = class_optim_warn

                all_charges.append((class_optim, "%s/%s\t" % (mission_charge, missions_charge[mission_id][month[1]])))
            else:
                # No charge planned of forecasted, display nothing for this month
                all_charges.append(("", ""))
        results.append([mission_link, _("All"), *all_charges])
        results.append([""] * 2 + [None, ""] * len(staffing_dates))  # Zero content row (hack for bold line)
        results.append([mark_safe("&nbsp;")] * 2 + [[None, mark_safe("&nbsp;")]] * len(staffing_dates))  # Empty row
    for consultant in consultants:
        consultant_link = mark_safe("<a href='%s#tab-staffing'>%s</a>" % (consultant.get_absolute_url(), escape(consultant)))
        all_charges = []
        for month in staffing_dates:
            consultant_charge = sum(
                solver.Value(staffing[consultant.trigramme][mission.mission_id()][month[1]]) for mission in missions)
            if consultants_freetime[consultant.trigramme][month[1]] - consultant_charge < 2:
                class_optim = class_optim_warn
            else:
                class_optim = class_optim_ok
            all_charges.append((class_optim, "%s/%s" % (consultant_charge, consultants_freetime[consultant.trigramme][month[1]])))
        results.append([_("All missions"), consultant_link, *all_charges])

    return results, missions_remaining_results


def compute_consultant_freetime(consultants, missions, months, projections="full"):
    """Compute freetime except for missions we want to plan
    projections: none, balanced or full. Similar to pdc review concept. Use mission probability"""
    freetime = {}
    holidays_days = Holiday.objects.all().values_list("day", flat=True)
    wdays = {month[0]: working_days(month[0], holidays_days) for month in months}
    for consultant in consultants:
        freetime[consultant.trigramme] = {}
        for month in months:
            current_staffings = consultant.staffing_set.filter(staffing_date=month[0], mission__probability__gt=0).exclude(mission__in=missions)
            current_staffings = current_staffings.select_related()
            if projections == "none":
                current_staffings = current_staffings.filter(mission__probability=100)
            charge = 0
            for staffing in current_staffings:
                if projections == "full":
                    charge += staffing.charge
                else:
                    charge += staffing.charge * staffing.mission.probability / 100
            freetime[consultant.trigramme][month[1]] = max(0, int(wdays[month[0]] - charge))
    return freetime


def compute_consultant_rates(consultants, missions):
    """Get or estimate consultant rates for given missions"""
    rates = {}
    for mission in missions:
        mission_rates = mission.consultant_rates()
        for consultant in consultants:
            if consultant.trigramme not in rates:
                rates[consultant.trigramme] = {}
            if consultant in mission_rates:
                rates[consultant.trigramme][mission.mission_id()] = mission_rates[consultant][0]
            else:  # use objective rate if rate is not defined at mission level
                consultant_rate = consultant.get_rate_objective(rate_type="DAILY_RATE")
                if consultant_rate:
                    rates[consultant.trigramme][mission.mission_id()] = consultant_rate.rate
                else:
                    rates[consultant.trigramme][mission.mission_id()] = 0
    return rates


@transaction.atomic
def solver_apply_forecast(solver, staffing, consultants, missions, staffing_dates, user):
    """Apply solver solution to staffing forecast"""
    now = datetime.now().replace(microsecond=0)  # Remove useless microsecond
    for mission in missions:
        mission_id = mission.mission_id()
        # Remove previous staffing for this mission after first month
        Staffing.objects.filter(mission=mission, staffing_date__gte=staffing_dates[0][0]).delete()
        # Create new staffing according to solver solution
        for consultant in consultants:
            for month in staffing_dates:
                charge = solver.Value(staffing[consultant.trigramme][mission_id][month[1]])
                if charge > 0:
                    s = Staffing(mission=mission, consultant=consultant,
                                 staffing_date=month[0], charge=charge,
                                 update_date=now, last_user=str(user))
                    s.save()
