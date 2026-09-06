#!/usr/bin/env python3
"""
Solution for finding column headers of smallest value greater than zero in each row.
"""

import openpyxl
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True, help='Input Excel file')
    parser.add_argument('--output', required=True, help='Output Excel file')
    args = parser.parse_args()

    # Load workbook
    wb = openpyxl.load_workbook(args.input)
    ws = wb.active
    
    # The data columns are B through N (columns 2 through 14)
    # Headers are in row 1
    # We need to fill P2:P7 with formulas
    
    # For LibreOffice Calc, we need to create a formula that:
    # 1. Finds the minimum value > 0 in the row
    # 2. Returns all column headers where this minimum appears
    # 3. Concatenates them with commas (no spaces)
    
    # The minimum value is already calculated in column O
    # We need to find which columns match this minimum
    
    for row_idx in range(2, 8):  # Rows 2-7
        # Create a formula that concatenates all matching column headers
        # We'll use TEXTJOIN if available, or build a complex IF formula
        
        # For LibreOffice Calc, we can use TEXTJOIN
        # TEXTJOIN(delimiter, ignore_empty, text1, [text2], ...)
        
        # We need to check each column B-N and if it equals the min value in O, include its header
        # Build an array formula approach
        
        # Since TEXTJOIN with array conditions might not work well, let's build a concatenation formula
        # that checks each column individually
        
        parts = []
        for col_idx in range(2, 15):  # Columns B through N (2 through 14)
            col_letter = openpyxl.utils.get_column_letter(col_idx)
            header_cell = f"${col_letter}$1"
            value_cell = f"{col_letter}{row_idx}"
            min_cell = f"$O{row_idx}"
            
            # IF(AND(value_cell=min_cell, value_cell>0), header_cell, "")
            parts.append(f'IF(AND({value_cell}={min_cell},{value_cell}>0),{header_cell},"")')
        
        # Now concatenate all parts with commas, but only if they're not empty
        # We need to build a formula that concatenates non-empty results with commas
        
        # One approach: use CONCATENATE with IF statements
        # But we need to handle the comma placement carefully
        
        # Let's use a different approach: build a formula that checks each column
        # and concatenates with comma only when needed
        
        # For simplicity and compatibility, let's build a nested IF formula
        # that concatenates results, adding commas between non-empty values
        
        # Actually, let's try TEXTJOIN which is available in LibreOffice Calc
        # TEXTJOIN(",", TRUE, array_of_conditions)
        
        # Build the formula using TEXTJOIN
        if_conditions = []
        for col_idx in range(2, 15):  # Columns B through N
            col_letter = openpyxl.utils.get_column_letter(col_idx)
            header_cell = f"${col_letter}$1"
            value_cell = f"{col_letter}{row_idx}"
            min_cell = f"$O{row_idx}"
            
            if_conditions.append(f'IF(AND({value_cell}={min_cell},{value_cell}>0),{header_cell},"")')
        
        # Join all IF conditions with TEXTJOIN
        formula = f'=TEXTJOIN(",",TRUE,{",".join(if_conditions)})'
        
        ws[f'P{row_idx}'] = formula
    
    # Save the workbook
    wb.save(args.output)
    print(f"Solution saved to {args.output}")


if __name__ == '__main__':
    main()
