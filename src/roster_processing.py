import pandas as pd

team_info = {'Down to like 40 pounds of Rice (now named Zimothy)':'Isaac',
             'Wild Card Chris':'Chris',
             'Lamar Jar Binks':'Sonny',
             'Shedeur for ROTY':'Marcus',
              'Prestige Worldwide':'Rimler',
             'Stuffed Crust and Olives Must':'Ricky',
             'Skattman John':'Pechman',
             'Marvin Gaye & Charbonnet':'Jake',
             'The Tone Zone':'Nick',
             'Portland Pizza Pies':'Luke'}

df = pd.read_excel("../data/raw/2025 draft picks.xlsx")
records = []

# create a dict of rosters and picks keyed by the team name
# i starts at 0, ends at total number of columns in df, and has a step of 2
for i in range(0, df.shape[1], 2):
    team = df.columns[i]
    owner = team_info[team]   
    players = df.iloc[1:, i]
    picks   = df.iloc[1:, i + 1]
    for player, pick in zip(players, picks):
        if pd.isna(player):           # skip blank rows at the bottom of shorter rosters
            continue
        records.append({
            "Team": team,
            "Owner":owner,
            "Player": player,
            "Draft_pick": pick,
        })
        
rosters = pd.DataFrame(records)
rosters.loc[rosters['Team']=='Down to like 40 pounds of Rice (now named Zimothy)' , 'Team'] = 'Zimbo Baggins'
rosters['Draft_pick'] = rosters['Draft_pick'].fillna('Undrafted')
rosters['Draft_pick'] = rosters['Draft_pick'].replace('-','Undrafted')
rosters['Player'].str.split(" - " , n=1 ,expand=True)
rosters[['left','right']]= rosters['Player'].str.split(" - " , n=1 , expand=True)

rosters['Player_Name'] = rosters['left'].str.rsplit(n=1).str[0]

rosters['Player_Position'] = rosters['right'].str.split(n=1).str[0]
rosters[['pick1','pick2']]= rosters['Player'].str.split(" - " , n=1 , expand=True)

rosters[['round','pick']] =  rosters['Draft_pick'].str.split(", " , n=1 , expand=True)
rosters['round']=rosters['round'].str.split(n=1).str[1]
rosters['pick_abs']=rosters['pick'].str.split(n=1).str[1]

rosters['round'] = pd.to_numeric(rosters['round'], errors='coerce')
rosters['pick'] = pd.to_numeric(rosters['pick'].str.split(n=1).str[1], errors='coerce')
rosters['overall_pick'] = (rosters['round'] - 1) * 10 + rosters['pick']

rosters = rosters[['Team','Owner','Player_Name','Player_Position','round','overall_pick','Draft_pick']]

#rosters.to_csv('../data/processed/rosters_2025.csv' , index=False)