# Working on this repository

## No identifiable data in this repository

This repository is public. Nothing in it may identify the house or the
people in it: no real first names, no entity ids built from a person's or a
child's name, no device addresses (IEEE, MAC, serial), no pictures or plans
of the house, no street, postcode, coordinates, hostnames or LAN addresses,
and nothing that says which room a child sleeps in. That applies to code,
comments, READMEs, this file, tests and fixtures, tool scripts, commit
messages and pull request text.

Use placeholders: Morgan and Casey for the adults, Riley's Room for the
child's room, `light.<room>` and `camera.rileys_room_*` for ids. Generic
words that any house has (`lock.front_door`, Kitchen, Bedroom) are fine.
The real values stay where they are read from, the config entry and the
dashboard YAML on Home Assistant, and in the private house notes that are
never committed to a public repository. Before pushing, grep the diff for
names.
