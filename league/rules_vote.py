"""Pure logic for the rules ballot: counting votes and wording turnout.

Named `rules_vote` rather than `rules` for the reason league/poll.py's docstring
sets out: views.py already has a `rules` view function, and
`from . import rules` followed by `def rules(...)` would shadow the module with
the function in a way that fails baffingly far from the cause.

Nothing here touches the database or the request -- plain values in, plain
values out, which is what lets the whole file be tested without a client, a
login or a fixture.

Note what is NOT here: anything that decides whether a proposal passed. Rules
section 8 says "majority vote" and does not say majority of what -- the league,
or the votes cast. Inventing a quorum rule in code would mean the app declaring
PASSED on a 4-3 split with three abstentions, and the argument that followed
would be the app's fault. The commissioner records the outcome by hand; see
RulesProposal.outcome.
"""


def count(choices):
    """{'for': n, 'against': n, 'abstain': n, 'cast': n} for one proposal.

    `choices` is whatever iterable of choice strings the caller has. A team that
    has not voted contributes nothing -- but a team that abstained contributes an
    'abstain', because on this ballot (unlike the draft poll) an abstention is a
    stored row and silence is not. See RulesVote's docstring.

    `cast` excludes abstentions: it is how many teams took a position, which is
    the number the two sides are actually compared against. It is a count and
    nothing more -- there is deliberately no threshold and no 'passed' key.
    """
    counts = {'for': 0, 'against': 0, 'abstain': 0}
    for choice in choices:
        if choice in counts:
            counts[choice] += 1

    counts['cast'] = counts['for'] + counts['against']
    return counts


def turnout(team_count, voted_count):
    """"7 of 10 managers have voted" -- the one wording of that sentence.

    In one place so the ballot page and the admin summary cannot spell it
    differently, which is the sort of drift nobody notices and everybody
    half-remembers wrong.
    """
    verb = 'has' if voted_count == 1 else 'have'
    return f'{voted_count} of {team_count} managers {verb} voted'
