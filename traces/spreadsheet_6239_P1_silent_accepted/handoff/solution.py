#!/usr/bin/env python3
"""
Solution for incentive calculation based on Goal, Variance to Goal, and Metric2
"""
import openpyxl
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    
    # Load workbook
    wb = openpyxl.load_workbook(args.input)
    ws = wb['Sheet1']
    
    # For rows 2-19 (Emp1 to Emp18), calculate incentive in column G
    for row in range(2, 20):
        # The formula needs to:
        # 1. Determine Goal bucket (which column in J:M)
        # 2. Look up the variance thresholds for that Goal bucket
        # 3. Determine which variance range (which row 3-6)
        # 4. Determine Metric2 bucket (which column in P:T)
        # 5. Look up payout from the intersection of variance row and metric2 column
        
        # Using nested IFs to determine the goal bucket and variance range, then metric2 bucket
        formula = f'''=IF(B{row}="Not Eligible","",
IF(D{row}<0.65,"",
IF(D{row}<0.75,
    IF(E{row}<=J$3,
        IF(F{row}<=-0.2,P$3,IF(F{row}<-0.1,Q$3,IF(F{row}<0,R$3,IF(F{row}<0.1,S$3,IF(F{row}<0.2,T$3,T$3))))),
        IF(E{row}<=J$4,
            IF(F{row}<=-0.2,P$4,IF(F{row}<-0.1,Q$4,IF(F{row}<0,R$4,IF(F{row}<0.1,S$4,IF(F{row}<0.2,T$4,T$4))))),
            IF(E{row}<=J$5,
                IF(F{row}<=-0.2,P$5,IF(F{row}<-0.1,Q$5,IF(F{row}<0,R$5,IF(F{row}<0.1,S$5,IF(F{row}<0.2,T$5,T$5))))),
                IF(F{row}<=-0.2,P$6,IF(F{row}<-0.1,Q$6,IF(F{row}<0,R$6,IF(F{row}<0.1,S$6,IF(F{row}<0.2,T$6,T$6)))))))),
IF(D{row}<0.85,
    IF(E{row}<=K$3,
        IF(F{row}<=-0.2,P$3,IF(F{row}<-0.1,Q$3,IF(F{row}<0,R$3,IF(F{row}<0.1,S$3,IF(F{row}<0.2,T$3,T$3))))),
        IF(E{row}<=K$4,
            IF(F{row}<=-0.2,P$4,IF(F{row}<-0.1,Q$4,IF(F{row}<0,R$4,IF(F{row}<0.1,S$4,IF(F{row}<0.2,T$4,T$4))))),
            IF(E{row}<=K$5,
                IF(F{row}<=-0.2,P$5,IF(F{row}<-0.1,Q$5,IF(F{row}<0,R$5,IF(F{row}<0.1,S$5,IF(F{row}<0.2,T$5,T$5))))),
                IF(F{row}<=-0.2,P$6,IF(F{row}<-0.1,Q$6,IF(F{row}<0,R$6,IF(F{row}<0.1,S$6,IF(F{row}<0.2,T$6,T$6)))))))),
IF(D{row}<0.95,
    IF(E{row}<=L$3,
        IF(F{row}<=-0.2,P$3,IF(F{row}<-0.1,Q$3,IF(F{row}<0,R$3,IF(F{row}<0.1,S$3,IF(F{row}<0.2,T$3,T$3))))),
        IF(E{row}<=L$4,
            IF(F{row}<=-0.2,P$4,IF(F{row}<-0.1,Q$4,IF(F{row}<0,R$4,IF(F{row}<0.1,S$4,IF(F{row}<0.2,T$4,T$4))))),
            IF(E{row}<=L$5,
                IF(F{row}<=-0.2,P$5,IF(F{row}<-0.1,Q$5,IF(F{row}<0,R$5,IF(F{row}<0.1,S$5,IF(F{row}<0.2,T$5,T$5))))),
                IF(F{row}<=-0.2,P$6,IF(F{row}<-0.1,Q$6,IF(F{row}<0,R$6,IF(F{row}<0.1,S$6,IF(F{row}<0.2,T$6,T$6)))))))),
    IF(E{row}<=M$3,
        IF(F{row}<=-0.2,P$3,IF(F{row}<-0.1,Q$3,IF(F{row}<0,R$3,IF(F{row}<0.1,S$3,IF(F{row}<0.2,T$3,T$3))))),
        IF(E{row}<=M$4,
            IF(F{row}<=-0.2,P$4,IF(F{row}<-0.1,Q$4,IF(F{row}<0,R$4,IF(F{row}<0.1,S$4,IF(F{row}<0.2,T$4,T$4))))),
            IF(E{row}<=M$5,
                IF(F{row}<=-0.2,P$5,IF(F{row}<-0.1,Q$5,IF(F{row}<0,R$5,IF(F{row}<0.1,S$5,IF(F{row}<0.2,T$5,T$5))))),
                IF(F{row}<=-0.2,P$6,IF(F{row}<-0.1,Q$6,IF(F{row}<0,R$6,IF(F{row}<0.1,S$6,IF(F{row}<0.2,T$6,T$6)))))))))))))'''
        
        ws[f'G{row}'] = formula
    
    # Save the workbook
    wb.save(args.output)

if __name__ == '__main__':
    main()
