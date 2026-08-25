# fulcra-computed-data-types

Turns a raw data export into something you can actually ask questions about.

The exports you can download from the services you use — listening history, purchases, activity logs — arrive as large, flat files. They contain the answer to "which artists did I actually listen to this year", but not in a form anything can query.

This skill writes the script that closes that gap. You point it at an export and tell it the dimension you care about — artist, genre, merchant, category — and it generates Python that parses the file, tags each record along that dimension, and ingests the result into Fulcra as a computed data type.

From then on the data behaves like any other Fulcra type: queryable, taggable, and available to any agent you have given access, rather than sitting in a download folder.

Includes a template to adapt and a worked example built from a Spotify export.
