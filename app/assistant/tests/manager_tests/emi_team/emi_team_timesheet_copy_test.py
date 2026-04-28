import time
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def main(manager_type, task, info=None):
    logger.info("initialize_system() is running...")
    print("initialize system...")
    factory = DI.multi_agent_manager_factory

    preload_start = time.time()
    manager_registry = DI.manager_registry
    manager_registry.preload_all()
    preload_end = time.time()
    elapsed_time = preload_end - preload_start
    logger.info("✅ Preloading completed in %.2f seconds.", elapsed_time)
    print(f"✅ Preloading completed in {elapsed_time:.2f} seconds.")

    logger.info("🔄 Creating %s...", manager_type)
    manager = factory.create_manager(manager_type)

    request_message = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content="",
        task=task,
        information=info,
    )
    result = manager.request_handler(request_message)
    print(result)


if __name__ == "__main__":
    formatting_rules = (
        "Time Entry Narrative Style Guide\n\n"
        "1) What a \"time entry block\" is\n"
        "- A time entry block contains one or more narratives.\n"
        "- Narratives describe work performed in present, active voice (imperative verb lead).\n\n"
        "Examples of acceptable verb leads:\n"
        "- Prepare, develop, determine, evaluate, perform, analyze, validate, reconcile, integrate, standardize.\n\n"
        "2) Formatting rules\n\n"
        "A) Single narrative block\n"
        "Format\n"
        "- One sentence.\n"
        "- No semicolon.\n"
        "- Ends with a period.\n"
        "- No time in parentheses inside the sentence.\n"
        "- Optional: include time after the period in brackets for clarity.\n\n"
        "Example\n"
        "- Attend analytics team meeting. [ ]\n\n"
        "B) Multi narrative block (2+ narratives)\n"
        "Format\n"
        "- Narratives separated by semicolon + space: \"; \"\n"
        "- Only the first narrative starts with a capital letter.\n"
        "- Every narrative after the semicolon starts with a lowercase letter.\n"
        "- Each narrative includes time in parentheses at the end, immediately before the semicolon or period: \"( )\"\n"
        "- The final narrative ends with a period.\n\n"
        "Example\n"
        "- Prepare analysis environment ( ); integrate payroll data into the analysis framework ( ).\n\n"
        "3) Voice and grammar rules\n\n"
        "A) Present active voice\n"
        "- Use active, present tense, verb-first phrasing.\n"
        "- Avoid \"I\", \"we\", and passive constructions.\n\n"
        "Prefer\n"
        "- Prepare methodology to...\n"
        "- Develop framework to...\n"
        "- Evaluate data to...\n"
        "- Perform analysis to...\n\n"
        "Avoid\n"
        "- Wrote code to...\n"
        "- Ran scripts to...\n"
        "- Was able to...\n"
        "- Strategy / strategize (unless truly strategic).\n\n"
        "B) No adjectives that claim effort or scope\n"
        "- Avoid words like: comprehensive, thorough, robust, extensive.\n"
        "- Clients assume those by default.\n\n"
        "C) Avoid \"simple task descriptions\"\n"
        "Do not describe the mechanics of the work (especially coding) unless it is truly the work product.\n\n"
        "Replace\n"
        "- \"write and run code to clean X\"\n\n"
        "With\n"
        "- \"prepare methodology for interpreting, standardizing, and comparing X\"\n\n"
        "4) Content rules (what to say)\n\n"
        "A) Emphasize expertise and methodology\n"
        "Each narrative should communicate:\n"
        "- Analytical intent (why the work matters)\n"
        "- Methodology/framework (how it is approached at a professional level)\n"
        "- Business/legal purpose (exposure, violations, claims/defenses, class metrics)\n\n"
        "B) Use \"methodology\" language when appropriate\n"
        "Common, reusable patterns:\n"
        "- Prepare methodology for...\n"
        "- Develop methodology to evaluate...\n"
        "- Determine assumptions, variables, and analytical framework for...\n"
        "- Perform quality control assessment of...\n"
        "- Prepare alternate methodology for...\n\n"
        "C) Avoid stating low-level implementation unless necessary\n"
        "Generally avoid:\n"
        "- write code, run code, pull data, move files, clean up columns\n\n"
        "Prefer:\n"
        "- integrate, reconcile, standardize, validate, assess, evaluate, develop, prepare\n\n"
        "5) Rewrite patterns (Bad -> Good)\n\n"
        "Coding and cleaning\n"
        "- Bad: Write code to bring the timecard data to a form where analysis can be run.\n"
        "  Good: Prepare timecard data for use in violation analysis and in assessment of potential exposure.\n\n"
        "- Bad: write and run code to clean census, timecard and payroll data.\n"
        "  Good: Prepare methodologies for interpreting, standardizing, and comparing timecard, payroll, and census data sets.\n\n"
        "\"Set up\" language\n"
        "- Bad: Set up analysis environment and customize the configuration.\n"
        "  Good: Determine assumptions, variables, and specific methodologies for data analysis and interpretation framework.\n\n"
        "Missing data handling\n"
        "- Bad: Strategize how to handle missing employees...\n"
        "  Good: Perform quality control analysis on potentially missing data sources.\n\n"
        "Extrapolation / imputation\n"
        "- Bad: use statistical methods to extrapolate... then impute...\n"
        "  Good: Prepare supplemental statistical extrapolation of baseline case metrics and potential violations.\n\n"
        "Alternate assumptions\n"
        "- Bad: perform another exposure analysis by assuming...\n"
        "  Good: Prepare alternate violation analysis methodology for missing shift data.\n\n"
        "\"Methodology for analyzing...\"\n"
        "- Bad: Conduct analysis of identifiers and their recurrences within the payroll dataset.\n"
        "  Good: Prepare methodology for analyzing identifiers and their recurrences within the payroll dataset.\n\n"
        "Standardized \"harmonize\" rewrite\n"
        "- Bad: Harmonize timecard data for further analysis.\n"
        "  Good: Prepare methodology for interpretation and standardization of timecard data for further analysis.\n\n"
        "6) Standard templates you can reuse\n\n"
        "Setup / environment\n"
        "- Prepare analysis environment ( ).\n"
        "- Prepare analysis methodology in assessment of claims and defenses ( ).\n\n"
        "Data integration / preprocessing\n"
        "- Integrate payroll data into exposure analysis framework ( ).\n"
        "- Prepare methodology for interpretation and standardization of timecard data for further analysis ( ).\n"
        "- Prepare timecard and census data for use in violation analysis and in assessment of potential exposure ( ).\n\n"
        "Core analysis\n"
        "- Perform analysis of potential facial violations on timecard data set ( ).\n"
        "- Develop methodology to evaluate timecard data set for potential data issues in support of exposure and violation analysis ( ).\n\n"
        "Quality control / validation\n"
        "- Perform quality control analysis on potentially missing data sources ( ).\n"
        "- Prepare methodology for quality control assessment of payroll rounding data ( ).\n\n"
        "Class and PAGA metrics\n"
        "- Perform data analysis and validation of class member metrics pertaining to employment tenure, separation dates, bonus pay, and workweek data during designated class and PAGA periods ( ).\n\n"
        "7) Fully formatted examples\n\n"
        "Single narrative\n"
        "- Attend analytics team meeting. [ ]\n\n"
        "Multi narrative\n"
        "- Prepare analysis environment ( ); develop methodology to evaluate timecard data set for potential data issues in support of exposure and violation analysis ( ).\n\n"
        "Longer multi narrative\n"
        "- Prepare analysis of payroll type identifiers, their prevalence and potential outliers ( ); perform data analysis and validation of class member metrics pertaining to employment tenure, separation dates, bonus pay and workweek data during designated class and PAGA periods ( ).\n\n"
        "Single narrative examples\n\n"
        "Attend analytics team meeting. [ ]\n\n"
        "Develop methodology for assessing and integrating new timecard data with punch indicators into analysis framework. [ ]\n\n"
        "Perform facial meal and rest violation analysis of the sample timecard data. [ ]\n\n"
        "Develop methodology for full sample of the timecard where IDs match previous production. [ ]\n\n"
        "Multi narrative examples (2 to 3 narratives)\n\n"
        "Prepare visualization of rounding time analysis ( ); prepare upper estimates of RROP violations due to bonus payments ( ); prepare visual and interactive calculator tool of potential damages and exposure for various itemized allegations ( ).\n\n"
        "Enter time on Carpe Diem for previous week ( ); edit narratives and enter time on Carpe Diem for October ( ); attend analytics team meeting ( ).\n\n"
        "Study the structure of time edit data ( ); collaboratively plan methodology for analysis of time edit data ( ).\n\n"
        "Perform collaborative quality assurance of the damages analysis results ( ); plan methodology for further damages assessments ( ).\n\n"
        "Multi narrative examples (longer blocks)\n\n"
        "Integrate new timecard data ( ); establish updated data ranges and confirm coverage and assess alignment for direct comparison ( ); perform analysis of overtime and double time payments using revised data, including updated identification of overpayments ( ); evaluate meal period compliance using revised violation counts and waiver assumptions, and reconcile identified penalties against recorded payments ( ); compute upper-bound unpaid wage scenario using revised hourly rate and updated assumptions for unpaid overtime work ( ); prepare waiting time penalty estimate based on assumed unpaid final wages and applicable penalty period ( ); update analysis outputs and structured workbooks to reflect findings based on revised source data ( ).\n\n"
        "Integrate timecard data with punch type column into the analysis framework ( ); perform analysis of facial meal and rest violations with the new time data ( ); perform quality assurance analysis of outliers and anomalies ( ); prepare summary of results ( ).\n\n"
        "Analyze employee counts across pay periods and evaluate workforce stability using change metrics ( ); prepare extrapolation framework for estimating employee growth and turnover beyond data endpoint ( ); prepare methodology for identification and extrapolation of current and former employee populations based on activity thresholds and separation logic ( ); analyze and extrapolate check counts and pay periods issued using timecard and payroll records ( ); evaluate completeness of payroll data coverage ( ); calculate average and median hourly rates based on payroll data ( ); identify and extrapolate frequency of meal premium payments during the PAGA period ( ); prepare sample of timecard, payroll, and attestation data ( ).\n\n"
        "\"Analysis + extrapolation\" examples (tight, reusable pattern)\n\n"
        "Analyze timecard and payroll datasets to assess employee population characteristics during the relevant period ( ); extrapolate results to estimate projected employee count through end of scope ( ).\n\n"
        "Analyze hire and termination activity to evaluate workforce turnover dynamics ( ); extrapolate results to estimate projected terminations and net change in workforce size ( ).\n\n"
        "Analyze payroll issuance patterns to assess check count and pay period coverage ( ); extrapolate results to estimate total checks issued through extrapolation period ( ).\n\n"
        "Analyze premium pay metrics to assess observed meal and rest premium hours ( ); extrapolate results to estimate potential exposure through end of period ( ).\n"
    )
    task = (
        "1) Read the dates in this file that are marked in form as *1/2/26*. "
        "2) For each date marker, read the text between this marker and the next marker "
        "(or EOF for the last one). "
        "3) For each date write the narratives for each company you find notes for.\n"
        "For each time entry block write the date followed by the name of the entity eg.:  2/7/26 Seyfarth: <time entry block here as discussed in formatting>."
        "Sometimes notes for some days are bad and there is no good way to tell what was done.  Rather than guess simply write the date and name of entity if known and note in brackets [Notes are lacking human assistance needed]."
        "File path: tasks/sample_timesheet.txt\n\n"
        "Formatting rules:\n"
        f"{formatting_rules}"
    )
    main("emi_team_manager", task)
