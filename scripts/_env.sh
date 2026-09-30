# Shared by the backtest scripts: load settings from a .env file.
#
# Settings (HA_URL, HA_TOKEN, TZ, HSEM_*) may live in a .env file so the token
# never has to be typed into a shell or committed.  The file is *parsed*, not
# sourced: a token containing $, quotes or backticks is taken literally and
# nothing in the file is executed.  Variables already exported win, so a
# one-off override on the command line still works.
#
# Usage, from a script in scripts/:
#     source "${REPO_ROOT}/scripts/_env.sh"
#     load_env_file "${ENV_FILE}"
load_env_file() {
    local file="$1" line key value
    [[ -f "${file}" ]] || return 0
    if [[ -n "$(find "${file}" -perm /077 2>/dev/null)" ]]; then
        echo "[warn] ${file} is readable by other users -- run: chmod 600 ${file}" >&2
    fi
    while IFS= read -r line || [[ -n "${line}" ]]; do
        line="${line#"${line%%[![:space:]]*}"}"          # trim leading space
        [[ -z "${line}" || "${line}" == \#* ]] && continue
        line="${line#export }"
        [[ "${line}" == *=* ]] || continue
        key="${line%%=*}"
        value="${line#*=}"
        [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        # Strip one pair of matching surrounding quotes.
        if [[ "${value}" =~ ^\"(.*)\"$ || "${value}" =~ ^\'(.*)\'$ ]]; then
            value="${BASH_REMATCH[1]}"
        fi
        [[ -n "${!key+x}" ]] && continue                    # exported value wins
        export "${key}=${value}"
    done < "${file}"
}
