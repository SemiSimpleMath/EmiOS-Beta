# Shared by pre-commit and pre-push (sourced, not run): reads the gitignored
# forbidden list into three arrays. See forbidden_strings.txt.example for the format.
#
#   forbidden_contacts  plain entries: contact details (emails, street address),
#                       matched as case-insensitive substrings
#   forbidden_phones    phone: entries, as extended regexes matching the number
#                       in any common format (310 555 0100, (310) 555-0100,
#                       310.555.0100, 3105550100); a regex because the same
#                       number is written many ways and a substring catches one
#   forbidden_words     word: entries: names, case-sensitive whole words
#
# pre-commit checks all three against a commit's added lines; pre-push checks
# contacts and phones against the whole tree being pushed.

forbidden_list="$(git rev-parse --show-toplevel)/scripts/git-hooks/forbidden_strings.txt"
forbidden_contacts=()
forbidden_phones=()
forbidden_words=()

# No list -> all three stay empty: a fresh clone legitimately has none yet.
load_forbidden() {
    [ -f "$forbidden_list" ] || return 0
    local line digits
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in
            ''|'#'*) continue ;;
            word:*)
                [ -n "${line#word:}" ] && forbidden_words+=("${line#word:}")
                ;;
            phone:*)
                digits="$(printf '%s' "${line#phone:}" | tr -cd '0-9')"
                if [ "${#digits}" -ne 10 ]; then
                    echo "forbidden list: a phone: entry must have 10 digits: $line" >&2
                    exit 1
                fi
                forbidden_phones+=("(^|[^0-9])\\(?${digits:0:3}\\)?[ .-]?${digits:3:3}[ .-]?${digits:6:4}([^0-9]|\$)")
                ;;
            *)
                forbidden_contacts+=("$line")
                ;;
        esac
    done < "$forbidden_list"
}
