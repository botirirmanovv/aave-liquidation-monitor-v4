"""Plan B used to be GC=F. Now other stocks — see xau_jam.spare."""
from xau_jam.spare import BOOK_B, main

SYMBOL = BOOK_B[0][0]
TRIGGER = BOOK_B[0][1]

if __name__ == "__main__":
    raise SystemExit(main())
