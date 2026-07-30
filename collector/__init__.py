"""Collection of GitHub review and contribution statistics.

The package is layered so that each concern can be read and tested on its own:

    constants   measured API limits, thresholds, and the published shape
    errors      the single error type raised when collection cannot continue
    redact      keeping identifying detail out of messages
    windows     dates, windows, and the slicing of searches
    queries     GraphQL documents and the search strings that fill them
    collector   the collection paths that fetch every record
    policy      loading and validating the classification policy
    classify    deciding what language a file is, from its path alone
    aggregate   accumulating classified records into language totals
    assemble    building the two payloads from collected records
    metrics     figures derived from a payload
    guards      the checks that must pass before anything is written
    report      human-readable summaries
    collect     orchestration and the command line
"""
